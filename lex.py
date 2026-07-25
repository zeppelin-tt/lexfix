#!/usr/bin/env python3
"""Управление словарём корректора. Правки применяются сразу, рестарт не нужен.

  ./lex.py fix «эдиторс» Editors   — жёсткое правило: всегда исправлять так
  ./lex.py add Nine Inch Nails     — научить новому имени (даёт fuzzy-исправления)
  ./lex.py block kiss              — запретить трогать это слово
  ./lex.py test «включи editers»   — прогнать текст, посмотреть результат
  ./lex.py why editers             — показать кандидатов и решение
  ./lex.py list                    — что добавлено руками
  ./lex.py rm editers              — убрать своё правило
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import translit  # noqa: E402
from corrector import (LEARNED_PATH, TECH_TERMS_PATH, Corrector,  # noqa: E402
                       _norm, _norm_any, FUZZY_AUTO, update_learned)


def key_of(s: str) -> str:
    """Ключ правила: кириллица сохраняется, латиница нормализуется как раньше."""
    return _norm_any(s) if translit.has_cyrillic(s) else _norm(s)


def _load_learned() -> dict:
    try:
        return json.loads(LEARNED_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def cmd_fix(args: list[str]) -> None:
    """Жёсткое правило: что слышится -> как писать."""
    if len(args) < 2:
        sys.exit("нужно два аргумента: ./lex.py fix «как слышится» «как писать»")
    wrong, right = args[0], " ".join(args[1:])
    key = key_of(wrong)
    if not key:
        sys.exit(f"«{wrong}» — не слово, правило не построить")
    update_learned(lambda data: {**data, key: right})
    print(f"✓ «{wrong}» → «{right}»")


def cmd_add(args: list[str]) -> None:
    """Новое собственное имя в словарь — чинит и опечатки в нём."""
    name = " ".join(args).strip()
    if not name:
        sys.exit("нужно имя: ./lex.py add «Nine Inch Nails»")
    existing = TECH_TERMS_PATH.read_text(encoding="utf-8") if TECH_TERMS_PATH.exists() else ""
    if any(line.strip().lower() == name.lower() for line in existing.splitlines()):
        print(f"«{name}» уже в словаре")
        return
    with TECH_TERMS_PATH.open("a", encoding="utf-8") as fh:
        fh.write(f"{name}\n")
    print(f"✓ «{name}» добавлено в tech_terms.txt")
    import build_lexicon
    build_lexicon.main()


def cmd_block(args: list[str]) -> None:
    """Запретить корректору трогать слово."""
    word = " ".join(args).strip()
    key = key_of(word)
    if not key:
        sys.exit("нужно слово: ./lex.py block kiss")
    update_learned(lambda data: {**data, key: None})
    print(f"✓ «{word}» больше не трогаем")


def cmd_rm(args: list[str]) -> None:
    key = key_of(" ".join(args))
    if key not in _load_learned():
        sys.exit(f"правила для «{key}» нет — посмотри ./lex.py list")

    def _rm(data: dict) -> dict:
        data.pop(key, None)
        return data

    update_learned(_rm)
    print(f"✓ правило для «{key}» убрано")


def cmd_list(_args: list[str]) -> None:
    data = _load_learned()
    if not data:
        print("правил пока нет")
        return
    fixes = {k: v for k, v in data.items() if v is not None}
    blocked = [k for k, v in data.items() if v is None]
    if fixes:
        print(f"исправления ({len(fixes)}):")
        for k, v in sorted(fixes.items()):
            print(f"  {k:24} → {v}")
    if blocked:
        print(f"\nне трогаем ({len(blocked)}):")
        for k in sorted(blocked):
            print(f"  {k}")


def cmd_test(args: list[str]) -> None:
    text = " ".join(args)
    if not text:
        sys.exit("нужен текст: ./lex.py test «включи editers»")
    result = Corrector(llm_enabled=True).correct(text)
    if result == text:
        print(f"без изменений: {text}")
    else:
        print(f"было:  {text}\nстало: {result}")


def explain_cyrillic(c: Corrector, phrase: str, key: str) -> None:
    """Разбор кириллического слова: почему оно стало (или не стало) названием."""
    from corrector import TRANSLIT_MIN_SKELETON, TRANSLIT_MIN_RATIO

    if key in c.blocked:
        print(f"«{phrase}»: в списке «не трогать» (./lex.py rm {key} — снять)")
        return
    if key in c.learned:
        print(f"«{phrase}»: ручное правило → «{c.learned[key]}»")
        return

    words = phrase.lower().split()
    lat = translit.to_latin(phrase.lower())
    sk = translit.skeleton(phrase)
    print(f"«{phrase}»: транслит → {lat}, скелет согласных → {sk or '—'}")

    short = [w for w in words if len(w) < 4]
    if short:
        print(f"→ слишком короткое ({', '.join(short)}) — такие не трогаем.\n"
              f"  Нужно всё равно: ./lex.py fix «{phrase}» «Название»")
        return

    plain = [w for w in words if w in c.ru_stop and w not in c.homonyms]
    if plain:
        print(f"→ обычное русское слово ({', '.join(plain)}) — защищено от подмены.\n"
              f"  Если это всё-таки название: ./lex.py fix «{phrase}» «Название»")
        return

    if len(sk) < TRANSLIT_MIN_SKELETON:
        print("→ слишком мало согласных, надёжно сопоставить не с чем.\n"
              f"  Нужно: ./lex.py fix «{phrase}» «Название»")
        return

    keys = c.by_skeleton.get(sk, [])
    if not keys:
        print("→ в словаре нет имени с таким набором согласных.\n"
              "  Научить: ./lex.py add «Правильное Имя»")
        return

    from difflib import SequenceMatcher
    scored = sorted(((k, SequenceMatcher(None, lat, k).ratio()) for k in keys),
                    key=lambda x: -x[1])
    print("  кандидаты по скелету:")
    for k, ratio in scored[:6]:
        mark = " " if ratio >= TRANSLIT_MIN_RATIO else "×"
        print(f"   {mark} {ratio:.3f}  {c.lexicon[k]['name']} ({c.lexicon[k]['kind']})")
    if not any(r >= TRANSLIT_MIN_RATIO for _, r in scored):
        print(f"→ все ниже порога {TRANSLIT_MIN_RATIO} — оставляем как есть")
        return

    homonym = [w for w in words if w in c.homonyms]
    if homonym:
        print(f"→ омоним ({', '.join(homonym)}): и русское слово, и название.\n"
              "  Решает LLM по контексту каждый раз заново — поэтому в одной фразе\n"
              "  заменится, а в другой нет. Зафиксировать намертво:\n"
              f"  ./lex.py fix «{phrase}» «Название»  или  ./lex.py block {phrase}")
    else:
        print("→ выбор отдаётся LLM-reranker'у (результат не запоминается, см. ./lex.py fix)")


def cmd_why(args: list[str]) -> None:
    """Почему слово исправилось так или не исправилось вовсе."""
    phrase = " ".join(args)
    key = key_of(phrase)
    if not key:
        sys.exit("нужно слово")
    c = Corrector(llm_enabled=False)
    if translit.has_cyrillic(phrase):
        explain_cyrillic(c, phrase, key)
        return

    if key in c.blocked:
        print(f"«{phrase}»: в списке «не трогать» (./lex.py rm {key} — снять)")
        return
    if key in c.learned:
        print(f"«{phrase}»: ручное правило → «{c.learned[key]}»")
        return
    if key in c.lexicon:
        e = c.lexicon[key]
        print(f"«{phrase}»: точное совпадение → «{e['name']}» "
              f"({e['kind']}, вес {e['weight']})")
        return

    if len(key) < 4:
        print(f"«{phrase}»: короче 4 символов — такие не трогаем (слишком легко "
              f"«исправить» во что угодно).\nНужно всё равно: ./lex.py fix {key} «Правильно»")
        return
    if " " not in key and key in c.english:
        print(f"«{phrase}»: обычное английское слово, в словаре имён его нет — "
              f"оставляем как есть.\nЕсли это всё-таки имя: ./lex.py add «{phrase}»")
        return

    cands = c._candidates(key)
    if not cands:
        print(f"«{phrase}»: похожих имён в словаре нет, оставляем как есть.\n"
              f"Научить: ./lex.py add «Правильное Имя»")
        return
    print(f"«{phrase}»: кандидаты —")
    for k, ratio in cands:
        print(f"  {ratio:.3f}  {c.lexicon[k]['name']} (вес {c.lexicon[k]['weight']})")
    if len(cands) == 1 and cands[0][1] >= FUZZY_AUTO:
        print(f"→ единственный и уверенный (≥{FUZZY_AUTO}) — применяется без LLM")
    else:
        print("→ выбор отдаётся LLM-reranker'у (результат не запоминается — "
              "ошибку reranker'а не хочется превращать в вечное правило; "
              "если он угадывает верно и хочется закрепить: ./lex.py fix)")


COMMANDS = {"fix": cmd_fix, "add": cmd_add, "block": cmd_block, "rm": cmd_rm,
            "list": cmd_list, "test": cmd_test, "why": cmd_why}

_QUOTE_PAIRS = {"«": "»", '"': '"', "'": "'"}


def _unquote(s: str) -> str:
    """Снимает обрамляющие «…»/"…"/'…', только если ОБА конца — пара:
    `.strip("«»\"'")` до этого срезал бы конечный апостроф и у «Guns N' Roses»."""
    if len(s) >= 2 and s[0] in _QUOTE_PAIRS and s[-1] == _QUOTE_PAIRS[s[0]]:
        return s[1:-1]
    return s


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(0 if len(sys.argv) < 2 else 1)
    COMMANDS[sys.argv[1]]([_unquote(a) for a in sys.argv[2:]])


if __name__ == "__main__":
    main()
