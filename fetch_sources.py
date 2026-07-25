#!/usr/bin/env python3
"""Тянет открытые базы имён собственных в vocab/ — программирование и музыкальное железо.

Требует интернет, запускается редко (раз в полгода, когда захочется освежить).
Сам корректор работает офлайн: он читает уже собранный lexicon.json.

  python3 fetch_sources.py          — обновить всё
  python3 fetch_sources.py music    — только музыкальное железо
"""

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VOCAB = Path(__file__).parent / "vocab"
SPARQL = "https://query.wikidata.org/sparql"
LINGUIST = "https://raw.githubusercontent.com/github-linguist/linguist/main/lib/linguist/languages.yml"
RUSSIAN_WORDS = "https://raw.githubusercontent.com/danakt/russian-words/master/russian.txt"
UA = "lexfix-lexicon-builder/1.0 (github.com/zeppelin-tt/lexfix)"

# Классы Wikidata. Наличие статьи в Wikidata — сам по себе фильтр известности,
# ровно то, что нужно: «самые известные», а не всё подряд.
WIKIDATA_SETS = {
    "dev_languages": ["Q9143"],                     # язык программирования
    "dev_software": [
        "Q1330336",   # веб-фреймворк
        "Q21127166",  # JavaScript-библиотека
        "Q3839507",   # библиотека (программирование)
        "Q9143195",   # интегрированная среда разработки
        "Q193351",    # СУБД
        "Q170584",    # система контроля версий... уточняется ниже по факту
    ],
    "music_gear": [
        "Q163829",    # синтезатор
        "Q212071",    # драм-машина
        "Q78987",     # электронный музыкальный инструмент
        "Q13487153",  # секвенсор
    ],
}

# Мусор, который попадает в выборки: слишком общее, слишком короткое,
# либо обычное слово, которое сломает распознавание русской речи.
BLOCKLIST = {
    "reason", "logic", "live", "one", "sound", "music", "audio", "midi", "studio",
    "system", "sample", "sampler", "organ", "piano", "guitar", "drum", "drums",
    "keyboard", "computer", "software", "program", "language", "library", "list",
    "set", "box", "core", "base", "data", "code", "test", "build", "run", "go",
    "d", "c", "r", "b", "j", "v", "q", "e", "k", "m", "s", "x", "t",
}


def _get(url: str, params: dict | None = None) -> bytes:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.read()


def _sparql(query: str, attempts: int = 4) -> list[dict]:
    """WDQS периодически лимитирует до 1 запроса в минуту — ждём и повторяем."""
    for attempt in range(attempts):
        try:
            raw = _get(SPARQL, {"query": query, "format": "json"})
            return json.loads(raw)["results"]["bindings"]
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503) or attempt == attempts - 1:
                print(f"  ! Wikidata: {e}")
                return []
            print(f"  … лимит Wikidata, жду 65 с (попытка {attempt + 2}/{attempts})")
            time.sleep(65)
        except Exception as e:
            print(f"  ! Wikidata недоступна: {e}")
            return []
    return []


def sparql_labels(qids: list[str]) -> set[str]:
    """Английские названия сущностей класса и всех его подклассов."""
    values = " ".join(f"wd:{q}" for q in qids)
    query = f"""
    SELECT DISTINCT ?l WHERE {{
      VALUES ?class {{ {values} }}
      ?i wdt:P31/wdt:P279* ?class .
      ?i rdfs:label ?l . FILTER(lang(?l) = "en")
    }}"""
    return {r["l"]["value"] for r in _sparql(query)}


def sparql_manufacturers(qids: list[str]) -> set[str]:
    """Производители — вытаскиваются через P176 у самих устройств.

    Отдельного удобного класса «производитель синтезаторов» в Wikidata нет,
    зато у каждого прибора проставлен изготовитель: Moog, Roland, Vermona.
    """
    values = " ".join(f"wd:{q}" for q in qids)
    query = f"""
    SELECT DISTINCT ?l WHERE {{
      VALUES ?class {{ {values} }}
      ?i wdt:P31/wdt:P279* ?class .
      ?i wdt:P176 ?m .
      ?m rdfs:label ?l . FILTER(lang(?l) = "en")
    }}"""
    return {r["l"]["value"] for r in _sparql(query)}


def linguist_languages() -> set[str]:
    """Языки программирования из GitHub Linguist — ключи верхнего уровня в YAML."""
    try:
        text = _get(LINGUIST).decode()
    except Exception as e:
        print(f"  ! Linguist недоступен: {e}")
        return set()
    return {m.group(1).strip() for m in re.finditer(r"^([A-Za-z][^\n:]*):$", text, re.M)}


def clean(names: set[str]) -> list[str]:
    out = set()
    for n in names:
        n = n.strip()
        # Уточнения в скобках — «Rust (programming language)» → «Rust»
        n = re.sub(r"\s*\([^)]*\)\s*$", "", n).strip()
        if not n or len(n) > 40:
            continue
        if n.lower() in BLOCKLIST:
            continue
        # Нужна латиница; чисто числовые и односимвольные имена бесполезны
        if not re.search(r"[A-Za-z]", n) or len(n) < 2:
            continue
        if re.fullmatch(r"[\d\W]+", n):
            continue
        out.add(n)
    return sorted(out)


def write(name: str, names: set[str], note: str) -> None:
    VOCAB.mkdir(exist_ok=True)
    items = clean(names)
    path = VOCAB / f"{name}.txt"
    path.write_text(f"# {note}\n# Сгенерировано fetch_sources.py — правки затрутся.\n"
                    f"# Своё добавляй в tech_terms.txt.\n" + "\n".join(items) + "\n")
    print(f"  {path.name}: {len(items)}")


def do_russian() -> None:
    """Словарь русских словоформ — стоп-лист для транслитерационного слоя.

    Без него любое русское слово рискует «опознаться» как английское имя.
    Кладём сжатым: build_lexicon.py выжмет из него компактный ru_stop.txt.
    """
    import gzip
    print("русский словарь:")
    try:
        raw = _get(RUSSIAN_WORDS)
    except Exception as e:
        print(f"  ! недоступен: {e}")
        return
    words = raw.decode("cp1251", errors="ignore").split()
    VOCAB.mkdir(exist_ok=True)
    path = VOCAB / "russian_words.txt.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write("\n".join(w.strip().lower() for w in words if w.strip()))
    print(f"  {path.name}: {len(words)} словоформ")


def do_dev() -> None:
    print("программирование:")
    langs = sparql_labels(WIKIDATA_SETS["dev_languages"]) | linguist_languages()
    write("dev_languages", langs, "Языки программирования (Wikidata + GitHub Linguist)")
    write("dev_software", sparql_labels(WIKIDATA_SETS["dev_software"]),
          "Фреймворки, библиотеки, IDE, СУБД (Wikidata)")


def do_music() -> None:
    print("музыкальное железо:")
    gear = WIKIDATA_SETS["music_gear"]
    write("music_gear", sparql_labels(gear),
          "Синтезаторы, драм-машины, секвенсоры (Wikidata)")
    write("music_brands", sparql_manufacturers(gear),
          "Производители музыкального оборудования (Wikidata, через P176)")


def main() -> None:
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("all", "dev"):
        do_dev()
    if what in ("all", "music"):
        do_music()
    if what in ("all", "russian"):
        do_russian()
    print("\nдальше: python3 build_lexicon.py")


if __name__ == "__main__":
    main()
