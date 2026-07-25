"""Коррекция англоязычных вкраплений в русском транскрипте GigaAM.

Четыре слоя, от дешёвого к дорогому:

  1. Точное совпадение со словарём (lexicon.json) — чиним регистр, 0 мс.
  2. Fuzzy-match — кандидаты по редакционному расстоянию; уверенный
     одиночный кандидат применяется сразу.
  3. LLM как reranker — получает контекст и пронумерованный список
     кандидатов, возвращает ОДНУ цифру через structured output. Модель
     физически не может вернуть свободный текст, поэтому испортить
     транскрипт ей нечем.
  4. Ремонт русифицированного жаргона — «закаметь» → «закоммитить». Здесь
     цель кириллическая, и слои 1-3 бессильны принципиально: ключ словаря
     строится только из латиницы (build_lexicon.norm), поэтому русское
     слово в словаре не представимо вообще. Единственный слой, где модель
     сочиняет текст свободно, — и потому единственный, чей ответ проходит
     через механический фильтр (см. _accept_jargon).

Слои 1-3 правят только латинские токены и кириллические записи английских
ИМЁН («флаттер» → Flutter); слой 4 — единственный, кто выдаёт кириллицу.
Принятые исправления копятся в learned.json и на следующий раз срабатывают
уже на слое 1.
"""

import array
import bisect
import fcntl
import json
import logging
import os
import re
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path

import translit

log = logging.getLogger("corrector")

HERE = Path(__file__).parent
LEXICON_PATH = HERE / "lexicon.json"
LEARNED_PATH = HERE / "learned.json"
LEARNED_LOCK_PATH = HERE / "learned.json.lock"
TECH_TERMS_PATH = HERE / "tech_terms.txt"
RU_STOP_PATH = HERE / "ru_stop.txt"
HOMONYMS_PATH = HERE / "homonyms.txt"
RU_WORDS_PATH = HERE / "ru_words.bin"
ENGLISH_WORDS_PATH = Path("/usr/share/dict/words")

OLLAMA_URL = "http://localhost:11434/api/chat"
LLM_MODEL = "qwen2.5:7b"
LLM_TIMEOUT = 20.0
# Ollama по умолчанию выгружает модель через 5 минут простоя, и следующая
# диктовка ждёт ~2 с на её загрузку. Час держит модель горячей при обычной
# работе и освобождает 4.7 ГБ, если про диктовку забыли.
LLM_KEEP_ALIVE = "1h"
LLM_VARIANTS = 5       # сколько своих догадок просить для списка в попапе

MAX_NGRAM = 3          # «Nick Cave and the Bad Seeds» не поймаем, «Pink Floyd» — да
MIN_TOKEN_LEN = 4      # короткие токены слишком легко «исправить» во что угодно
FUZZY_CUTOFF = 0.75    # ниже — не кандидат
FUZZY_AUTO = 0.88      # выше и кандидат единственный — применяем без LLM
MAX_CANDIDATES = 5
COMMON_WORD_WEIGHT = 50  # артист популярнее — перебивает обычное английское слово

# Кириллический слой (см. translit.py) намеренно строже латинского: спутать
# русское слово с английским именем куда легче, чем опечатку с оригиналом.
TRANSLIT_ENABLED = True
TRANSLIT_MIN_SKELETON = 3    # скелет короче — слишком много коллизий
TRANSLIT_MIN_RATIO = 0.55    # согласные сошлись, но гласные должны быть похожи
TRANSLIT_AUTO_RATIO = 0.95   # почти точное попадание — можно без LLM;
                             # ниже порога идут падежные формы («телеграме»),
                             # где решение лучше доверить контексту
# Аббревиатуры диктуют по буквам («эс-ку-эль»), а не как слово, поэтому
# кириллическое слово почти никогда не является записью аббревиатуры. Зато
# скелет согласных у них совпадает со всем подряд: «модель» → mdl → MDL.
# Пропускаем их только при почти точном совпадении — «абба» → ABBA.
TRANSLIT_ACRONYM_RATIO = 0.9
# Кириллический слой ходит только по проверенным источникам. Сваленный
# из GitHub Linguist список «языков» содержит SKILL, BASIC, Vision, Parser —
# на латинице это безобидно (там мы чиним опечатку в уже английском слове),
# а на кириллице превращает «скилл» в «SKILL» и «парсер» в «Parser».
TRANSLIT_KINDS = frozenset({"tech", "dev_curated", "music_curated",
                            "music_brands", "music_gear"})
TRANSLIT_ARTIST_WEIGHT = 30   # артистов пускаем, только если реально слушает

# Слой 4: обрусевшие англицизмы — «закоммитить», «запушить», «задеплоить».
# Словарём это не лечится: перечислять пришлось бы произведение двух больших
# множеств (грамматические формы × способы, которыми GigaAM их коверкает), а
# правильная форма ещё и зависит от фразы — «надо закаметь» → «закоммитить»,
# но «вчера закамитил» → «закоммитил».
# Первая версия (условие входа «слова нет в словаре словоформ») на прогоне по
# реальному тексту дала 9 ложных срабатываний из 45 дошедших до модели — и все
# 9 оказались существительными («файлик» → «файловый», «Харланенкова» →
# «Харламов», «финтеха» → «финтех»), а все настоящие цели — глаголами
# («закаметь», «закамитил», «задиплоим», «запушыть»). Уменьшительные, фамилии
# и падежные формы обычных слов законно отсутствуют в словаре словоформ, а
# скелет-фильтр ловит дикую выдумку, но не уверенную правку того, что править
# не требовалось.
# Фильтр по глагольным окончаниям (см. _VERB_ENDING_RE) сузил вход до слов,
# морфологически похожих на глагол, — жаргонные существительные и фамилии под
# него не попадают вообще, ни разу не обращаясь к модели.
JARGON_ENABLED = True
JARGON_MIN_LEN = 5            # короче — слишком легко «починить» во что угодно
JARGON_MAX_LEN = 24
# Ответ модели принимается, только если скелет согласных почти не изменился.
# Это и есть замена схемы-ограничителя, которой слой 4 лишён: выдумка не по
# делу и любая инструкция, затесавшаяся в текст, скелет не проходят.
# «закаметь» (skmt) → «закоммитить» (skmtt) даёт 0.89 — нижняя граница среди
# настоящих целей. Порог 0.6 пропускал и откровенную выдумку: «листить» →
# «лескать» даёт 0.75 (первую букву не ловит — обе «л»), а «ревьюит» →
# «отревьюить» — 0.857. Оба разошедшихся случая ниже 0.889, поэтому порог
# поднят до 0.8 — с запасом ниже всех настоящих целей и выше обеих выдумок.
JARGON_SKELETON_RATIO = 0.8
JARGON_LEN_RATIO = (0.6, 1.8)  # «одно слово» на выходе, а не пересказ фразы
JARGON_CACHE_MAX = 512         # решения за жизнь процесса, см. _repair_jargon
# Дефис исключён намеренно: составных слов в списке словоформ нет, поэтому
# «какие-то», «каким-то», «санкт-петербурге», «код-ревью» проходили бы фильтр
# «нет в русском языке» и ехали к нейросети пачками (замерено на прогоне по
# 18k слов). Жаргонный глагол дефиса не содержит никогда — дешевле отсечь
# весь класс, чем разбирать его по частям.
_CYRILLIC_WORD_RE = re.compile(r"^[А-Яа-яЁё]+$")

# Все ложные срабатывания слоя 4 на прогоне по реальному тексту были
# существительными и фамилиями, все настоящие цели — глаголами. Окончание —
# дешёвая проверка морфологии до похода к модели: инфинитив (-ить/-ать/-еть/
# -ыть — сюда попадает и «закаметь», хотя правильная форма «закоммитить»
# оканчивается на «-ить»: у GigaAM гласная перед окончанием часто плывёт),
# прошедшее время (-ил/-ила/-или), настоящее/будущее (-ит/-им/-ишь/-ете/-ят).
_VERB_ENDING_RE = re.compile(
    r"(ить|ать|еть|ыть|ил|ила|или|ит|им|ишь|ете|ят)$", re.IGNORECASE)

# Источники, скачанные пачкой и никем не отсмотренные. В них попадают языки
# с именами «Max», «Mind», «PILOT», «BASIC» — обычные английские слова. Для
# таких записей требуем, чтобы слово не существовало в английском языке само
# по себе: «html» → HTML пройдёт, «pilot» → PILOT нет.
WEAK_KINDS = frozenset({"dev_languages", "dev_software",
                        "music_gear", "music_brands"})

_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё'’\-]*")
_LATIN_ONLY_RE = re.compile(r"^[A-Za-z'’\-\s]+$")

# GigaAM иногда мешает алфавиты внутри одного слова: «MК2» — M латинская,
# К кириллическая. Визуально неотличимо, но ломает любой поиск.
# Применяется ТОЛЬКО к словам со смешанными алфавитами: на обычном русском
# тексте такая замена превратила бы «Москва» в «MockBa».
_HOMOGLYPHS = str.maketrans("АВЕКМНОРСТУХаеорсух", "ABEKMHOPCTYXaeopcyx")
_PUNCT_RE = re.compile(r"[^a-zA-Z0-9\s]")
_SPACE_RE = re.compile(r"\s+")

# Слова, которые чаще произносятся как слова, а не как названия групп
# (Kiss, Air, Yes, Love, War — всё это ещё и артисты).
COMMON_EN = frozenset("""
a about after again all also always am an and any are as at back be because been
before being best better between big both but by call can come could day did do
does doing done down each even every first for from get give go going good got
great had has have he her here him his how i if in into is it its just know last
let like little long look made make man many may me more most much must my never
new next no not now of off often old on once one only or other our out over own
part people place put right said same say see she should since so some such take
tell than that the their them then there these they thing think this those
though through time to too two under up us use very want was way we well were
what when where which while who why will with without work world would year yes
you your love war air yes can kiss might not live one help hard fine
""".split())


def _norm(s: str) -> str:
    return _SPACE_RE.sub(" ", _PUNCT_RE.sub(" ", s.lower())).strip()


def _norm_any(s: str) -> str:
    """То же, но кириллица сохраняется — для ключей в learned.json."""
    return _SPACE_RE.sub(" ", re.sub(r"[^\w\s]", " ", s.lower(), flags=re.U)).strip()


def _fix_homoglyphs(text: str) -> str:
    """«MК2» → «MK2»: чинит слова, где смешаны латиница и кириллица."""
    def repl(m: re.Match) -> str:
        w = m.group()
        if re.search(r"[A-Za-z]", w) and re.search(r"[А-Яа-яЁё]", w):
            fixed = w.translate(_HOMOGLYPHS)
            # Заменяем, только если смешение действительно ушло.
            if not re.search(r"[А-Яа-яЁё]", fixed):
                return fixed
        return w
    return re.sub(r"[\w'’\-]+", repl, text, flags=re.U)


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _atomic_write_json(path: Path, data: dict) -> None:
    """temp-file + os.replace — читатель никогда не увидит недописанный файл
    (в отличие от write_text напрямую: SIGKILL/бросок питания посреди записи
    оставляет обрезанный JSON, и следующий _load_json тихо вернёт {})."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
    os.replace(tmp, path)


def update_learned(mutate) -> dict:
    """Читает learned.json, применяет mutate(dict) -> dict, атомарно пишет —
    всё под fcntl.flock на отдельном lock-файле.

    Три процесса пишут в один learned.json (сервер, виджет, lex.py): без
    блокировки classic lost-update — оба читают старую версию, оба пишут,
    один правки теряет. Лочим отдельный `.lock`-файл, а не сам learned.json:
    если лочить сам файл, который потом ещё и atomic-replace'ится, второй
    процесс может получить лок на уже отвязанный (старый) inode и прочитать
    данные до чужой правки — блокировка молча перестанет работать."""
    with open(LEARNED_LOCK_PATH, "a", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            data = mutate(_load_json(LEARNED_PATH))
            _atomic_write_json(LEARNED_PATH, data)
            return data
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


def _is_acronym(name: str) -> bool:
    """«MDL», «SQL», «ABBA» — пишется целиком капсом, произносится по буквам."""
    letters = [c for c in name if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters) and len(letters) <= 5


def _load_words(path: Path) -> frozenset:
    try:
        return frozenset(path.read_text(encoding="utf-8").split())
    except OSError:
        return frozenset()


class Corrector:
    def __init__(self, llm_enabled: bool = True):
        self.llm_enabled = llm_enabled
        self.english = self._load_english()
        self._ru_words_cache: array.array | None = None  # см. _ru_words()
        self._jargon_cache: dict[str, str | None] = {}   # см. _repair_jargon()
        self._mtimes: tuple = ()
        self._reload()
        log.info("corrector: %d записей словаря, %d выученных, англ. словарь %d слов",
                 len(self.lexicon), len(self.learned), len(self.english))

    @staticmethod
    def _stat() -> tuple:
        out = []
        for p in (LEXICON_PATH, LEARNED_PATH):
            try:
                out.append(p.stat().st_mtime_ns)
            except OSError:
                out.append(0)
        return tuple(out)

    def _reload(self) -> None:
        self.lexicon = _load_json(LEXICON_PATH)
        raw_learned = _load_json(LEARNED_PATH)
        # null в learned.json = «никогда не трогать это слово» (ручной блок).
        self.learned = {k: v for k, v in raw_learned.items() if v is not None}
        self.blocked = frozenset(k for k, v in raw_learned.items() if v is None)
        # Бакеты по числу слов — сужают перебор при fuzzy-поиске.
        self.by_wordcount: dict[int, list[str]] = {}
        for key in self.lexicon:
            self.by_wordcount.setdefault(key.count(" ") + 1, []).append(key)
        # Индекс скелетов согласных — вход для кириллического слоя.
        self.by_skeleton: dict[str, list[str]] = {}
        for key, entry in self.lexicon.items():
            if not (entry["kind"] in TRANSLIT_KINDS
                    or (entry["kind"] == "artist"
                        and entry["weight"] >= TRANSLIT_ARTIST_WEIGHT)):
                continue
            sk = translit.skeleton(entry["name"])
            if len(sk) >= TRANSLIT_MIN_SKELETON:
                self.by_skeleton.setdefault(sk, []).append(key)
        self.ru_stop = _load_words(RU_STOP_PATH)
        self.homonyms = _load_words(HOMONYMS_PATH)
        self._mtimes = self._stat()

    def _reload_if_changed(self) -> None:
        """Правки словаря подхватываются на лету, без рестарта сервера."""
        if self._stat() != self._mtimes:
            log.info("corrector: словарь изменился, перезагружаю")
            self._reload()

    @staticmethod
    def _load_english() -> frozenset:
        try:
            words = ENGLISH_WORDS_PATH.read_text(encoding="utf-8").split()
        except OSError:
            return frozenset()
        return frozenset(w.lower() for w in words)

    # --- слой 1: точное совпадение --------------------------------------

    def _exact(self, key: str) -> str | None:
        if key in self.learned:
            return self.learned[key]
        if key in self.blocked:
            return None
        entry = self.lexicon.get(key)
        if entry is None:
            return None
        # Одиночное слово, которое существует и как обычное английское
        # («logic», «vision», «free», «kiss»), капитализируем только если оно
        # заметно присутствует в коллекции — иначе это просто слово в тексте.
        if " " not in key and (key in COMMON_EN or key in self.english):
            if entry["kind"] in WEAK_KINDS:
                return None
            if entry["weight"] < COMMON_WORD_WEIGHT:
                return None
        return entry["name"]

    # --- слой 2: fuzzy ---------------------------------------------------

    def _candidates(self, key: str) -> list[tuple[str, float]]:
        bucket = self.by_wordcount.get(key.count(" ") + 1, [])
        n = len(key)
        scored = []
        for cand in bucket:
            if abs(len(cand) - n) > 3:
                continue
            m = SequenceMatcher(None, key, cand)
            if m.real_quick_ratio() < FUZZY_CUTOFF or m.quick_ratio() < FUZZY_CUTOFF:
                continue
            ratio = m.ratio()
            if ratio >= FUZZY_CUTOFF:
                scored.append((cand, ratio))
        scored.sort(key=lambda c: (-c[1], -self.lexicon[c[0]]["weight"]))
        return scored[:MAX_CANDIDATES]

    # --- слой 3: LLM выбирает из кандидатов ------------------------------

    def _llm_pick(self, phrase: str, context: str, options: list[str]) -> str | None:
        numbered = "\n".join(f"{i}. {o}" for i, o in enumerate(options, 1))
        prompt = (
            f"Расшифровка русской речи содержит английское слово, записанное с ошибкой.\n\n"
            f"Контекст: «{context}»\n"
            f"Ошибочное слово: «{phrase}»\n\n"
            f"Варианты правильного написания:\n{numbered}\n\n"
            f"Какой вариант имелся в виду? Ответь номером. "
            f"Если ни один не подходит — ответь 0."
        )
        return self._llm_choice(prompt, options)

    def _llm_request(self, prompt: str, fmt: dict, num_predict: int) -> dict | None:
        """Один запрос к Ollama со structured output. None — модель недоступна.

        Схема (`format`) — не украшение: она физически ограничивает форму
        ответа, поэтому свободный текст модель вернуть не может. Именно на
        этом держится надёжность слоя 3, см. CLAUDE.md.
        """
        payload = json.dumps({
            "model": LLM_MODEL,
            "stream": False,
            "keep_alive": LLM_KEEP_ALIVE,
            "options": {"temperature": 0, "num_predict": num_predict},
            "format": fmt,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        try:
            req = urllib.request.Request(
                OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
                return json.loads(json.loads(resp.read())["message"]["content"])
        except Exception as e:
            log.warning("LLM недоступна: %s", e)
            return None

    def _llm_choice(self, prompt: str, options: list[str]) -> str | None:
        """Ответ ограничен схемой: модель возвращает номер, не текст."""
        data = self._llm_request(
            prompt,
            {"type": "object", "properties": {"choice": {"type": "integer"}},
             "required": ["choice"]},
            16)
        if not data:
            return None
        try:
            choice = int(data["choice"])
        except (KeyError, TypeError, ValueError):
            return None
        if 1 <= choice <= len(options):
            return options[choice - 1]
        return None

    # --- кириллический слой ----------------------------------------------

    def _resolve_cyrillic(self, phrase: str, context: str) -> str | None:
        """«флаттер» → Flutter. Строже латинского пути: см. TRANSLIT_* выше."""
        words = phrase.lower().split()
        if any(len(w) < MIN_TOKEN_LEN for w in words):
            return None
        # Обычное русское слово не трогаем — кроме омонимов вроде «докер»,
        # «питон», «флаттер»: они и слова, и названия, решить может только
        # контекст, поэтому их пропускаем дальше, но строго через LLM.
        homonym = False
        for w in words:
            if w in self.ru_stop:
                if w in self.homonyms:
                    homonym = True
                else:
                    return None

        sk = translit.skeleton(phrase)
        if len(sk) < TRANSLIT_MIN_SKELETON:
            return None
        keys = self.by_skeleton.get(sk)
        if not keys:
            return None

        lat = translit.to_latin(phrase.lower())
        scored = []
        for k in keys:
            name = self.lexicon[k]["name"]
            floor = (TRANSLIT_ACRONYM_RATIO if _is_acronym(name)
                     else TRANSLIT_MIN_RATIO)
            ratio = SequenceMatcher(None, lat, k).ratio()
            if ratio >= floor:
                scored.append((k, ratio))
        if not scored:
            return None
        scored.sort(key=lambda c: (-c[1], -self.lexicon[c[0]]["weight"]))
        scored = scored[:MAX_CANDIDATES]

        if not homonym and len(scored) == 1 and scored[0][1] >= TRANSLIT_AUTO_RATIO:
            return self.lexicon[scored[0][0]]["name"]
        if not self.llm_enabled:
            return None
        options = [self.lexicon[k]["name"] for k, _ in scored]
        # Выбор LLM не запоминается: как и у омонимов (см. ниже), уверенность
        # тут ниже, чем у точного/fuzzy слоя, а одна ошибка reranker'а иначе
        # становится вечным жёстким правилом. См. `_resolve()` — тот же довод
        # для латинского пути, где это уже привело к мусору в learned.json
        # («stml» → HTML, «skills» → SKILL — реальные записи, найденные в
        # ревью 2026-07-25).
        return self._llm_pick_cyrillic(phrase, context, options, homonym)

    def _llm_pick_cyrillic(self, phrase: str, context: str, options: list[str],
                           homonym: bool = False) -> str | None:
        numbered = "\n".join(f"{i}. {o}" for i, o in enumerate(options, 1))
        if homonym:
            head = (f"«{phrase}» — это и обычное русское слово, и название "
                    f"(программа, инструмент, группа). Пойми по контексту, что "
                    f"имелось в виду.\n\n")
            tail = ("Если это название — ответь его номером. Если обычное русское "
                    "слово — ответь 0.")
        else:
            head = ("В расшифровке русской речи английское название записано "
                    "кириллицей, на слух.\n\n")
            tail = ("Какой вариант произносится так же и подходит по смыслу? "
                    "Ответь номером. Если это обычное русское слово, а не "
                    "название — ответь 0.")
        prompt = (f"{head}Контекст: «{context}»\nЗаписано как: «{phrase}»\n\n"
                  f"Варианты:\n{numbered}\n\n{tail}")
        return self._llm_choice(prompt, options)

    # --- разрешение одного кандидата-фразы -------------------------------

    # --- слой 4: русифицированный жаргон ----------------------------------

    def _ru_words(self) -> array.array:
        """Индекс русских словоформ — лениво, один раз на процесс.

        Не в `_reload()`, потому что 12 MB нужны только если слой 4 реально
        дошёл до дела: обычная диктовка про музыку его не трогает вовсе, а
        виджет в строке меню чаще закрывают, чем доводят до жаргона.

        Отдельный файл, а не ru_stop.txt: у них противоположные задачи.
        ru_stop отвечает «это слово похоже на имя из словаря, не подменяй»
        (103k слов), а тут нужно «такого слова в русском языке нет вовсе»,
        и на это отвечает только полный список словоформ.
        """
        if self._ru_words_cache is None:
            arr = array.array("Q")
            try:
                arr.frombytes(RU_WORDS_PATH.read_bytes())
            except OSError:
                log.info("нет ru_words.bin — слой жаргона молчит "
                         "(пересобрать: build_lexicon.py)")
            self._ru_words_cache = arr
        return self._ru_words_cache

    def _is_russian_word(self, word: str) -> bool:
        """Есть ли слово в русском языке. Пустой индекс → «да» для всего:
        слой 4 обязан молчать, когда проверить нечем, а не чинить вслепую."""
        arr = self._ru_words()
        if not arr:
            return True
        h = translit.ru_word_hash(word)
        i = bisect.bisect_left(arr, h)
        return i < len(arr) and arr[i] == h

    def _repair_jargon(self, phrase: str, context: str) -> str | None:
        """«закаметь» → «закоммитить»: англицизм с русской морфологией.

        Слои 1-3 такое не берут вообще — их цель всегда латинская, а здесь
        нужно кириллическое слово в правильной грамматической форме, которую
        без фразы не угадать.

        Условие входа — двойное и дешёвое, оба до похода к модели: слова нет в
        русском языке, и оно морфологически похоже на глагол (см.
        _VERB_ENDING_RE). Настоящие слова («заметить», «закатить», «закоптить»)
        первый фильтр не проходят и остаются нетронутыми; жаргонные
        существительные и фамилии («файлик», «Харланенкова») отсекает второй —
        именно они и были всеми ложными срабатываниями на прошлом прогоне.
        Мусор GigaAM («закаметь», «закамить») проходит оба. Так к нейросети
        попадают единицы токенов на транскрипт, а не каждый.
        """
        if not (JARGON_ENABLED and self.llm_enabled):
            return None
        word = phrase.strip()
        if " " in word or not (JARGON_MIN_LEN <= len(word) <= JARGON_MAX_LEN):
            return None
        if not _CYRILLIC_WORD_RE.match(word):
            return None
        low = word.lower()
        if not _VERB_ENDING_RE.search(low):
            return None
        if low in self.homonyms or self._is_russian_word(low):
            return None
        # Правильно написанный жаргон («ревьюер», «деплой», «конфиг») фильтр
        # входа не отсекает — его в русском словаре тоже нет, — и модель на
        # него честно отвечает «уже верно». Но в тексте такие слова
        # повторяются, и без кэша каждое повторение стоило бы отдельного
        # запроса: на прогоне по 18k слов «ревьюер» встретился 16 раз.
        # Кэш процессный, не learned.json: решение зависит от фразы, а
        # автозапись подтверждений тут когда-то уже привела к мусору в
        # словаре (см. историю про auto-learn).
        if low in self._jargon_cache:
            cached = self._jargon_cache[low]
            return self._match_case(word, cached) if cached else None
        guess = self._llm_jargon(word, context)
        fixed = self._accept_jargon(word, guess) if guess else None
        if len(self._jargon_cache) < JARGON_CACHE_MAX:
            self._jargon_cache[low] = fixed.lower() if fixed else None
        return fixed

    @staticmethod
    def _match_case(word: str, fixed: str) -> str | None:
        """Регистр исходника важнее: слово в начале предложения иначе стало бы
        строчным, и правку зарубил бы acceptable() уже в correct()."""
        if word[:1].isupper():
            fixed = fixed[:1].upper() + fixed[1:]
        return fixed if fixed != word else None

    def _llm_jargon(self, word: str, context: str) -> str | None:
        """Единственное место в автоматическом пути, где модель пишет текст.

        Схемой-ограничителем, как в `_llm_pick()`, тут не обойтись: правильного
        ответа нет ни в одном списке, его надо именно сочинить. Поэтому
        ограничение перенесено с формы ответа на его содержание — см.
        `_accept_jargon()`, без которого этот метод звать нельзя.
        """
        ctx = " ".join(context.split())[:200]
        context_line = f"Фраза: «{ctx}»\n" if ctx else ""
        prompt = (
            "Ты чинишь расшифровку русской речи, записанную на слух. Одно "
            "слово записано неправильно. Это профессиональный жаргон: "
            "английское слово, которым пользуются по-русски — с русскими "
            "приставками, суффиксами и окончаниями.\n\n"
            "Примеры:\n"
            "«закаметь» → закоммитить\n"
            "«закамитил» → закоммитил\n"
            "«задиплоить» → задеплоить\n"
            "«запушыть» → запушить\n"
            "«отрефакторить» → отрефакторить\n"
            # Без личной формы в примерах модель охотно сваливает всё в
            # инфинитив («смержым» → «смержить»). С ней хотя бы остаётся в
            # личной форме, хотя лицо и число может не угадать — на сильно
            # исковерканном окончании их и не восстановить, там уже гадание.
            "«пушым» → пушим\n\n"
            f"{context_line}"
            f"Записано: «{word}»\n\n"
            "Верни ОДНО слово — как оно должно писаться по-русски, кириллицей, "
            "в той же грамматической форме: то же время, лицо, число, та же "
            "приставка. Не переводи на английский и не пиши латиницей. Если "
            "слово уже написано правильно — верни его без изменений."
        )
        data = self._llm_request(
            prompt,
            {"type": "object", "properties": {"word": {"type": "string"}},
             "required": ["word"]},
            32)
        if not data or not isinstance(data.get("word"), str):
            return None
        return data["word"]

    @staticmethod
    def _accept_jargon(word: str, guess: str) -> str | None:
        """Механический фильтр поверх свободного ответа нейросети.

        Слой 4 — исключение из правила «модель не может вернуть свободный
        текст» (см. шапку модуля), поэтому гарантию даёт не схема ответа, а
        эта проверка. Она пропускает только слово, которое звучит как
        исходное: у выдумки не по делу и у инструкции, затесавшейся в текст,
        скелет согласных с оригиналом не сходится.
        """
        guess = guess.strip().strip(".,;:!?«»\"'")
        if not guess or not _CYRILLIC_WORD_RE.match(guess):
            return None
        lo, hi = JARGON_LEN_RATIO
        if not (lo <= len(guess) / len(word) <= hi):
            return None
        # Приставку GigaAM/Whisper слышат надёжно — гуляет обычно корень или
        # окончание. Несовпадение первой буквы — верный признак, что модель не
        # исправила слово, а придумала другое: «ревьюит» → «отревьюить»
        # (лишняя приставка «от-») даёт скелетное ratio 0.86, с запасом выше
        # порога, и без этой проверки проходит как «исправление».
        if guess[:1].lower() != word[:1].lower():
            log.info("жаргон: «%s» → «%s» отклонено, другая первая буква",
                     word, guess)
            return None
        ratio = SequenceMatcher(None, translit.skeleton(word),
                                translit.skeleton(guess)).ratio()
        if ratio < JARGON_SKELETON_RATIO:
            log.info("жаргон: «%s» → «%s» отклонено, скелет %.2f",
                     word, guess, ratio)
            return None
        return Corrector._match_case(word, guess)

    def _resolve(self, phrase: str, context: str) -> str | None:
        if translit.has_cyrillic(phrase):
            if not TRANSLIT_ENABLED:
                return None
            key = _norm_any(phrase)
            if key in self.blocked:
                return None
            if key in self.learned:
                return self.learned[key]
            fixed = self._resolve_cyrillic(phrase, context)
            if fixed and fixed != phrase:
                return fixed
            # Английского имени не нашлось — но слово может быть и не именем,
            # а обрусевшим глаголом («закаметь»). Это разные задачи с разными
            # целевыми алфавитами, поэтому разные слои, а не один общий.
            return self._repair_jargon(phrase, context)

        key = _norm(phrase)
        if not key or key in self.blocked:
            return None

        exact = self._exact(key)
        if exact is not None:
            return exact if exact != phrase else None

        if len(key) < MIN_TOKEN_LEN:
            return None
        # Нормальное английское слово, которого нет в словаре, — оставляем как есть:
        # выдумывать за пользователя опаснее, чем пропустить.
        if " " not in key and key in self.english:
            return None

        cands = self._candidates(key)
        if not cands:
            return None
        if len(cands) == 1 and cands[0][1] >= FUZZY_AUTO:
            best = self.lexicon[cands[0][0]]["name"]
            return best if best != phrase else None
        if not self.llm_enabled:
            return None

        options = [self.lexicon[c]["name"] for c, _ in cands]
        # Не запоминаем: LLM здесь спрашивают именно потому, что fuzzy-слой не
        # уверен (иначе сработал бы FUZZY_AUTO без LLM), а разовая ошибка
        # reranker'а на неоднозначном слове иначе становится ПОСТОЯННЫМ
        # правилом слоя 1 — не заметить такое легко, откатить руками (`lex.py
        # rm`) для этого и придумали. Явное `lex.py fix`/попап-виджет по
        # прежнему пишут в learned.json — это разные пути.
        picked = self._llm_pick(phrase, context, options)
        return picked if picked and picked != phrase else None

    # --- предложение без контекста (для виджета) --------------------------

    def suggest_variants(self, phrase: str, limit: int = 8,
                         context: str = "") -> list[tuple[str, str]]:
        """Несколько вариантов замены на выбор — для списка в попапе виджета.

        Возвращает `[(текст, источник)]`, где источник — «правило» (готовое
        правило из learned.json), «словарь» (точное/fuzzy/транслит-попадание)
        или «догадка» (свободное предложение нейросети). Порядок — от самого
        обоснованного к самому вольному.

        `context` — несколько слов вокруг фразы (виджет достаёт их через
        Accessibility API, см. menubar.py:_read_selection_context). Без
        контекста нейросеть видит только само слово и чаще ошибается на
        неоднозначных случаях — используется исключительно как подсказка
        для «догадки», на словарные слои (правило/точное/fuzzy) не влияет.

        Это единственное место, где догадки вообще появляются. В `correct()`
        их нет и быть не должно: там замена применяется сама, без человека,
        и цена выдумки — молча испорченный транскрипт. Здесь же вариант лишь
        показывается в списке, а меняется слово, только когда по нему кликнули.
        """
        self._reload_if_changed()
        phrase = phrase.strip()
        if not phrase:
            return []
        cyr = translit.has_cyrillic(phrase)
        key = _norm_any(phrase) if cyr else _norm(phrase)
        # `block` уважаем и здесь: раз сказано не трогать — не подсказываем.
        if not key or key in self.blocked:
            return []

        out: list[tuple[str, str]] = []
        rule = self.learned.get(key)
        if rule:
            out.append((rule, "правило"))
        out += [(name, "словарь") for name in self._dict_candidates(phrase, key, cyr)]

        if self.llm_enabled:
            # Жаргон идёт перед свободными догадками: _llm_variants ищет
            # английское название и на «закаметь» предложит что угодно
            # латиницей, а нужное тут — русское слово.
            jargon = self._repair_jargon(phrase, context)
            if jargon:
                out.append((jargon, "жаргон"))
            out += [(g, "догадка")
                    for g in self._llm_variants(phrase, [t for t, _ in out], context)]

        seen = {phrase.lower()}
        uniq: list[tuple[str, str]] = []
        for text, src in out:
            if text.lower() in seen:
                continue
            seen.add(text.lower())
            uniq.append((text, src))
        return uniq[:limit]

    def _dict_candidates(self, phrase: str, key: str, cyr: bool) -> list[str]:
        """Похожие имена из словаря — без порогов «применять/не применять».

        `_resolve()` те же кандидаты отбирает строго, потому что решает сам.
        Здесь решает человек, поэтому берём всё, что прошло базовое сходство,
        и просто показываем списком.
        """
        if cyr:
            sk = translit.skeleton(phrase)
            if len(sk) < TRANSLIT_MIN_SKELETON:
                return []
            lat = translit.to_latin(phrase.lower())
            scored = sorted(
                ((k, SequenceMatcher(None, lat, k).ratio())
                 for k in self.by_skeleton.get(sk, [])),
                key=lambda c: (-c[1], -self.lexicon[c[0]]["weight"]))
            return [self.lexicon[k]["name"] for k, r in scored[:MAX_CANDIDATES]
                    if r >= TRANSLIT_MIN_RATIO]

        names = []
        exact = self._exact(key)
        if exact:
            names.append(exact)
        names += [self.lexicon[k]["name"] for k, _ in self._candidates(key)]
        return names

    def _llm_variants(self, phrase: str, known: list[str],
                      context: str = "") -> list[str]:
        """Свободные догадки нейросети: опечатка это или транслитерация.

        Принципиально отличается от `_llm_pick()`: там модель выбирает номер
        из готового списка и выдумать своё физически не может, здесь — ровно
        наоборот, выдумывает. Допустимо только потому, что результат
        показывается человеку и применяется по клику.
        """
        skip = ", ".join(known[:6]) if known else "—"
        context_line = f"Контекст (соседние слова во фразе): «{context}»\n" if context else ""
        # Примеры тут не для красоты: без них qwen2.5:7b понимает задачу как
        # «переставь буквы» и возвращает мусор вроде «aifel tover» → «aibel
        # tower», «aefel tower». С примерами она начинает именно узнавать
        # задуманное слово, а это и требуется.
        prompt = (
            "Ты разбираешь расшифровку русской речи, записанную на слух. "
            "Слово исковеркано: либо орфографическая ошибка, либо английское "
            "название, переданное кириллицей по произношению.\n\n"
            "Твоя задача — УЗНАТЬ, какое реально существующее слово или "
            "название имелось в виду, и написать его правильно латиницей.\n\n"
            "Примеры:\n"
            "«эдиторс» → Editors\n"
            "«aifel tover» → Eiffel Tower\n"
            "«постгрес» → PostgreSQL, Postgres\n"
            "«вижуал студио» → Visual Studio\n\n"
            f"{context_line}"
            f"Записано: «{phrase}»\n"
            f"Уже предложено, не повторяй: {skip}\n\n"
            + ("Контекст выше — реальные слова вокруг «" + phrase + "» в фразе, "
               "используй их, чтобы понять смысл и выбрать подходящее по теме "
               "название, а не любое похожее по звучанию.\n\n" if context else "")
            + "Верни до 5 вариантов, самый вероятный первым. Каждый вариант "
            "обязан быть настоящим словом, названием или именем. НЕ предлагай "
            "простые перестановки букв исходного слова — они бесполезны. Если "
            "не узнаёшь слово — верни пустой список, это нормальный ответ."
        )
        data = self._llm_request(
            prompt,
            {"type": "object",
             "properties": {"variants": {"type": "array",
                                         "items": {"type": "string"}}},
             "required": ["variants"]},
            160)
        if not data or not isinstance(data.get("variants"), list):
            return []
        out = []
        for v in data["variants"]:
            # Схема гарантирует форму, но не содержимое: модель нет-нет да и
            # вернёт пояснение целой фразой вместо слова — такое отсекаем.
            if not isinstance(v, str):
                continue
            v = v.strip().strip(".,;")
            if v and len(v) <= 40 and "\n" not in v and len(v.split()) <= MAX_NGRAM:
                out.append(v)
        return out[:LLM_VARIANTS]

    # --- публичный вход ---------------------------------------------------

    def correct(self, text: str) -> str:
        self._reload_if_changed()
        if not text.strip() or not self.lexicon:
            return text

        text = _fix_homoglyphs(text)
        tokens = [(m.start(), m.end(), m.group()) for m in _TOKEN_RE.finditer(text)]
        if not tokens:
            return text

        # Соседние однородные токены (разделённые только пробелами/дефисами)
        # образуют span — внутри него ищем многословные названия. Латиница и
        # кириллица в один span не попадают: «Vermona перформер» разбирается
        # как два разных случая, каждый своим слоем.
        spans: list[list[int]] = []
        for i, (start, _, word) in enumerate(tokens):
            same_script = (spans and
                           translit.has_cyrillic(word) ==
                           translit.has_cyrillic(tokens[spans[-1][-1]][2]))
            if same_script and re.fullmatch(r"[\s\-]*", text[tokens[i - 1][1]:start]):
                spans[-1].append(i)
            else:
                spans.append([i])

        def acceptable(before: str, after: str) -> bool:
            """Отсекает замену, которая только портит регистр: «XCX» → «xcx».

            Одной первой буквы мало: акроним подлиннее может потерять капс
            в середине, а первая буква при этом останется заглавной —
            «API» → «api» первый символ не ловит вовсе. «API» → «Api»
            (заглавная только первая) — по-прежнему нормальный результат.
            """
            if before[:1].isupper() and after[:1].islower():
                return False
            letters = [c for c in before if c.isalpha()]
            if len(letters) >= 2 and all(c.isupper() for c in letters):
                return any(c.isupper() for c in after if c.isalpha())
            return True

        edits: list[tuple[int, int, str]] = []
        for span in spans:
            i = 0
            while i < len(span):
                for n in range(min(MAX_NGRAM, len(span) - i), 0, -1):
                    first, last = tokens[span[i]], tokens[span[i + n - 1]]
                    # Буквы, приклеенные к цифрам, — это хеш, версия или
                    # артикул: «9cc6b5», «v2», «85f288f8». Не наше дело.
                    if ((first[0] and text[first[0] - 1].isdigit())
                            or (last[1] < len(text) and text[last[1]].isdigit())):
                        continue
                    phrase = text[first[0]:last[1]]
                    ctx = text[max(0, first[0] - 60):min(len(text), last[1] + 60)]
                    fixed = self._resolve(phrase, ctx)
                    if fixed is not None and acceptable(phrase, fixed):
                        edits.append((first[0], last[1], fixed))
                        i += n
                        break
                else:
                    i += 1

        if not edits:
            return text
        out = []
        pos = 0
        for start, end, replacement in edits:
            out.append(text[pos:start])
            out.append(replacement)
            pos = end
        out.append(text[pos:])
        result = "".join(out)
        log.info("corrector: %d замен %s", len(edits),
                 [(text[s:e], r) for s, e, r in edits])
        return result
