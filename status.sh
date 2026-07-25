#!/bin/bash
# Проверка установки LexFix: словарь, подпись, процесс, Ollama.
# Запуск: bash status.sh
cd "$(dirname "$0")" || exit 1
export LC_ALL=en_US.UTF-8
ok=0; bad=0
say() { local n=${#1}; printf "  %s%*s %s\n" "$1" $((38 - n > 0 ? 38 - n : 1)) "" "$2"; }
good() { say "$1" "✓ $2"; ok=$((ok+1)); }
fail() { say "$1" "✗ $2"; bad=$((bad+1)); }

echo
echo "── Словарь ──"
for f in lexicon.json ru_stop.txt homonyms.txt; do
    if [ -s "$f" ]; then
        good "$f" "$(wc -l < "$f" | tr -d ' ') строк"
    else
        fail "$f" "нет — собери: venv/bin/python3 build_lexicon.py"
    fi
done

echo
echo "── Приложение ──"
if [ -d /Applications/LexFix.app ]; then
    if codesign --verify --strict /Applications/LexFix.app 2>/dev/null; then
        good "Подпись LexFix.app" "валидна"
    else
        fail "Подпись LexFix.app" "битая — Accessibility работать не будет"
        echo "      bash build_app.sh"
    fi
    if pgrep -f "LexFix.app/Contents/MacOS/LexFix" >/dev/null; then
        good "Процесс LexFix" "запущен"
    else
        fail "Процесс LexFix" "не запущен"
        echo "      launchctl kickstart -k gui/\$(id -u)/local.lex-widget"
    fi
else
    fail "LexFix.app" "не установлен — bash install.sh"
fi

echo
echo "── Ollama (опционально, для LLM-вариантов в попапе) ──"
if curl -s --max-time 3 http://localhost:11434/api/tags 2>/dev/null | grep -q "qwen2.5:7b"; then
    good "qwen2.5:7b" "установлена"
else
    fail "qwen2.5:7b" "не найдена — работает без LLM-вариантов (только словарь)"
    echo "      brew install ollama && brew services start ollama && ollama pull qwen2.5:7b"
fi

echo
if [ "$bad" -eq 0 ]; then
    echo "  Всё на месте."
else
    echo "  Проблем: $bad — команды для починки указаны выше."
fi
echo
