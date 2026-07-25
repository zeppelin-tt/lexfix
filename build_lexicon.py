#!/usr/bin/env python3
"""Собирает lexicon.json — словарь собственных имён для коррекции транскриптов.

Источники:
  - scrobbles.jsonl (Last.fm) — артисты, вес = число прослушиваний
  - tech_terms.txt — ручной список, вес фиксированный

Запуск: python3 build_lexicon.py
"""

import array
import gzip
import json
import os
import re
from collections import Counter
from pathlib import Path

import translit
from translit import ru_word_hash

HERE = Path(__file__).parent
# Слой музыкантов — опциональный и личный: он строится из истории прослушиваний
# Last.fm конкретного человека, которой в публичном репозитории нет и не будет.
# Без него собирается ровно тот же словарь минус имена артистов — остальные три
# источника (tech_terms.txt, vocab/*_curated.txt, vocab/dev_languages.txt)
# работают всегда. Свой scrobbles.jsonl подключается переменной окружения:
#   LEXFIX_SCROBBLES=~/path/to/scrobbles.jsonl python3 build_lexicon.py
# Формат — JSONL, одна прослушанная композиция на строку, ключ "artist".
_scrobbles_env = os.environ.get("LEXFIX_SCROBBLES")
SCROBBLES = Path(_scrobbles_env).expanduser() if _scrobbles_env else None
TECH_TERMS = HERE / "tech_terms.txt"
VOCAB = HERE / "vocab"
OUT = HERE / "lexicon.json"
RU_STOP_OUT = HERE / "ru_stop.txt"
HOMONYMS_OUT = HERE / "homonyms.txt"
RU_WORDS_OUT = HERE / "ru_words.bin"

# Слово-омоним похоже на техтермин настолько, что стоит спросить у LLM,
# что имелось в виду. Порог низкий намеренно: «питон»/python дают лишь 0.73,
# а ошибиться тут нельзя — решает всё равно LLM по контексту.
HOMONYM_RATIO = 0.7
HOMONYM_KINDS = {"tech", "dev_curated", "dev_languages", "dev_software",
                 "music_curated", "music_gear", "music_brands"}

TECH_WEIGHT = 500
VOCAB_WEIGHT = 300   # ниже ручных терминов: своё всегда важнее скачанного


def _atomic_write_text(path: Path, text: str) -> None:
    """temp-file + os.replace — сервер/виджет перечитывают эти файлы по mtime
    (см. Corrector._reload_if_changed), обрезанный файл посреди записи иначе
    ловится ими как «пустой словарь»."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)

# «Nick Cave and the Bad Seeds» дробить нельзя, а «Amon Tobin feat. Figueroa» — нужно.
_SPLIT_RE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring|vs\.?|versus|w/)\s+", re.I)
_LATIN_RE = re.compile(r"[a-zA-Z]")


def norm(s: str) -> str:
    """Ключ для поиска: только строчная латиница, цифры и одинарные пробелы."""
    s = re.sub(r"[^a-zA-Z0-9\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def usable(name: str) -> bool:
    """Отсев записей, от которых один вред.

    В Last.fm водятся артисты с именами «B!», «F*», «Cω», «A♯» — на них
    налипает что угодно, включая git-хеши: «9cc6b5» → «9cc6B!5».
    """
    letters = [c for c in name if c.isalpha()]
    if len(letters) < 3:
        return False
    # Имя, состоящее в основном из символов, а не букв, — почти всегда мусор.
    return len(letters) >= len(name) * 0.5


def artist_variants(raw: str) -> list[str]:
    """Полное имя плюс его части, если это коллаборация."""
    raw = raw.strip()
    if not raw or not _LATIN_RE.search(raw):
        return []
    parts = _SPLIT_RE.split(raw)
    out = [raw] if len(parts) > 1 else []
    out.extend(p.strip() for p in parts)
    return [p for p in out if p and _LATIN_RE.search(p)]


def collect_artists() -> Counter:
    weights: Counter = Counter()
    if SCROBBLES is None:
        print("! LEXFIX_SCROBBLES не задан, артисты пропущены")
        return weights
    if not SCROBBLES.exists():
        print(f"! нет {SCROBBLES}, артисты пропущены")
        return weights
    with SCROBBLES.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                artist = json.loads(line).get("artist")
            except (json.JSONDecodeError, AttributeError):
                continue
            if not artist:
                continue
            for variant in artist_variants(artist):
                weights[variant] += 1
    return weights


def _read_terms(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [t.strip() for t in lines if t.strip() and not t.startswith("#")]


def collect_tech() -> list[str]:
    return _read_terms(TECH_TERMS)


def collect_vocab() -> dict[str, list[str]]:
    """Скачанные базы из vocab/*.txt (см. fetch_sources.py)."""
    if not VOCAB.exists():
        return {}
    return {p.stem: _read_terms(p) for p in sorted(VOCAB.glob("*.txt"))}


def build_ru_stop(entries: dict) -> int:
    """Русские слова, которые рискуют быть принятыми за английское имя.

    Полный словарь — 1.5 млн словоформ, держать его в памяти сервера незачем:
    опасны только те слова, чей скелет согласных совпал со скелетом
    какого-нибудь имени из lexicon. Таких на порядки меньше.
    """
    src = VOCAB / "russian_words.txt.gz"
    if not src.exists():
        print("  ru_stop: нет vocab/russian_words.txt.gz "
              "(python3 fetch_sources.py russian) — транслитерация будет рискованной")
        return 0
    wanted = {translit.skeleton(e["name"]) for e in entries.values()}
    wanted.discard("")
    hits = set()
    with gzip.open(src, "rt", encoding="utf-8") as fh:
        for word in fh:
            word = word.strip()
            if len(word) < 4:
                continue
            if translit.skeleton(word) in wanted:
                hits.add(word)
    _atomic_write_text(RU_STOP_OUT, "\n".join(sorted(hits)) + "\n")
    return len(hits)


def build_ru_words() -> int:
    """Компактный индекс «это вообще русское слово?» — вход слоя жаргона.

    Отличается от ru_stop.txt задачей и потому охватом: ru_stop защищает от
    подмены те слова, что похожи на имена из словаря (57k), а здесь нужен
    противоположный вопрос — «такого слова в русском языке нет вовсе», и на
    него отвечает только полный список словоформ (1.5 млн).

    Хранится не строками, а отсортированными 64-битными хешами: набор строк
    занял бы 266 MB в памяти, массив хешей — 15 MB (замерено), а виджету в
    строке меню столько памяти брать не за что. Коллизия хеша безопасна по
    направлению: выдуманное слово будет ошибочно принято за настоящее и
    останется нетронутым — то есть худший исход это пропущенная правка, а не
    испорченный текст.

    Хеш обязан быть стабильным между запусками, поэтому blake2b, а не
    встроенный hash() — тот рандомизируется солью процесса.
    """
    src = VOCAB / "russian_words.txt.gz"
    if not src.exists():
        print("  ru_words: нет vocab/russian_words.txt.gz "
              "(python3 fetch_sources.py russian) — слой жаргона работать не будет")
        return 0
    with gzip.open(src, "rt", encoding="utf-8") as fh:
        hashes = array.array("Q", sorted(
            {ru_word_hash(w) for w in (line.strip() for line in fh) if w}))
    tmp = RU_WORDS_OUT.with_suffix(RU_WORDS_OUT.suffix + ".tmp")
    tmp.write_bytes(hashes.tobytes())
    os.replace(tmp, RU_WORDS_OUT)
    return len(hashes)


def build_homonyms(entries: dict, ru_stop: set[str]) -> int:
    """Русские слова, которые заодно являются названиями: «докер», «питон», «флаттер».

    Стоп-лист их справедливо защищает как обычные русские слова — но именно
    они чаще всего и нужны. Разрешить такое можно только по контексту, поэтому
    здесь мы лишь помечаем их: решение примет LLM в момент распознавания.
    Артисты не участвуют — их 10k, и шум от них перевесил бы пользу.
    """
    from difflib import SequenceMatcher
    by_skeleton: dict[str, list[str]] = {}
    for key, entry in entries.items():
        name = entry["name"]
        if entry["kind"] not in HOMONYM_KINDS:
            continue
        # Аббревиатуры («MDL», «SQL») диктуют по буквам — русским словом
        # они не записываются, а скелет согласных дают слишком ходовой.
        letters = [c for c in name if c.isalpha()]
        if letters and all(c.isupper() for c in letters) and len(letters) <= 5:
            continue
        sk = translit.skeleton(name)
        if len(sk) >= 3:
            by_skeleton.setdefault(sk, []).append(key)

    found = set()
    for word in ru_stop:
        keys = by_skeleton.get(translit.skeleton(word))
        if not keys:
            continue
        lat = translit.to_latin(word)
        if any(SequenceMatcher(None, lat, k).ratio() >= HOMONYM_RATIO for k in keys):
            found.add(word)
    _atomic_write_text(HOMONYMS_OUT, "\n".join(sorted(found)) + "\n")
    return len(found)


def main() -> None:
    artists = collect_artists()
    vocab = collect_vocab()
    tech = collect_tech()

    # Ключ -> лучшее написание. Приоритет снизу вверх:
    # скачанные базы < артисты по весу < свои термины.
    entries: dict[str, dict] = {}
    for source, names in vocab.items():
        for name in names:
            key = norm(name)
            if key and usable(name):
                entries[key] = {"name": name, "weight": VOCAB_WEIGHT, "kind": source}
    for name, weight in artists.items():
        key = norm(name)
        if not key or not usable(name):
            continue
        prev = entries.get(key)
        if prev is None or prev["kind"] not in ("tech",) and weight > prev["weight"]:
            entries[key] = {"name": name, "weight": weight, "kind": "artist"}
    for name in tech:
        key = norm(name)
        if key:
            entries[key] = {"name": name, "weight": TECH_WEIGHT, "kind": "tech"}

    _atomic_write_text(OUT, json.dumps(entries, ensure_ascii=False, indent=0))
    words = sum(1 for k in entries if " " not in k)
    print(f"lexicon.json: {len(entries)} записей "
          f"({words} однословных, {len(entries) - words} многословных)")
    print(f"  артистов: {len(artists)}, своих терминов: {len(tech)}")
    for source, names in sorted(vocab.items()):
        print(f"  {source}: {len(names)}")
    n = build_ru_stop(entries)
    if n:
        print(f"  ru_stop.txt: {n} русских слов, защищённых от подмены")
        ru_stop = set(RU_STOP_OUT.read_text(encoding="utf-8").split())
        h = build_homonyms(entries, ru_stop)
        print(f"  homonyms.txt: {h} слов-омонимов (решает LLM по контексту)")
    w = build_ru_words()
    if w:
        print(f"  ru_words.bin: {w} словоформ ({w * 8 / 1e6:.0f} MB) "
              f"— опознание жаргона")


if __name__ == "__main__":
    main()
