#!/bin/bash
# Полная установка LexFix с нуля: venv, зависимости, словарь, сертификат для
# подписи, сборка .app, LaunchAgent. Идемпотентен — можно запускать повторно,
# уже сделанные шаги пропускаются.
#
# Использование: bash install.sh
set -euo pipefail
cd "$(dirname "$0")"

CERT_NAME="LexFix Local Signing"
LAUNCH_LABEL="local.lex-widget"
PLIST="$HOME/Library/LaunchAgents/$LAUNCH_LABEL.plist"

say() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

if [[ "$(uname)" != "Darwin" ]]; then
    echo "LexFix — только macOS (AppKit/PyObjC, Accessibility API)." >&2
    exit 1
fi

# ── 1. Python 3.12 ────────────────────────────────────────────────────────
# Верхняя граница не принципиальна (в отличие от GigaAM-стороны проекта, тут
# нет зависимости от torch) — 3.12 просто протестирована и стабильна с py2app.
say "Python 3.12"
PY312=""
for candidate in python3.12 /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PY312="$(command -v "$candidate")"
        break
    fi
done
if [ -z "$PY312" ]; then
    if command -v brew >/dev/null 2>&1; then
        echo "python3.12 не найден — ставлю через Homebrew…"
        brew install python@3.12
        PY312="$(brew --prefix python@3.12)/bin/python3.12"
    else
        echo "Нужен Python 3.12 и Homebrew не найден. Поставь Homebrew" >&2
        echo "(https://brew.sh) или Python 3.12 (https://python.org) вручную," >&2
        echo "затем запусти install.sh снова." >&2
        exit 1
    fi
fi
echo "используется: $PY312 ($("$PY312" --version))"

# ── 2. venv + зависимости ─────────────────────────────────────────────────
say "venv и зависимости"
if [ ! -x venv/bin/python3 ]; then
    "$PY312" -m venv venv
fi
venv/bin/pip install --quiet --upgrade pip
venv/bin/pip install --quiet -r requirements.txt
echo "готово: venv/"

# ── 3. Словарь коррекции ──────────────────────────────────────────────────
# Работает и без интернета, и без личных данных: tech_terms.txt (свои термины)
# + vocab/*_curated.txt (кураторские списки) + vocab/dev_languages.txt уже
# лежат в репозитории. Слой музыкантов из Last.fm — опциональный, см.
# LEXFIX_SCROBBLES в README.md, по умолчанию пропускается.
say "Словарь коррекции"
if [ -s lexicon.json ] && [ -s ru_stop.txt ] && [ -s homonyms.txt ]; then
    echo "уже собран (lexicon.json, ru_stop.txt, homonyms.txt) — пропускаю"
    echo "пересобрать: venv/bin/python3 build_lexicon.py"
else
    venv/bin/python3 build_lexicon.py
fi

# ── 4. Сертификат для стабильной подписи ──────────────────────────────────
# Зачем вообще нужен свой сертификат, а не ad-hoc (`codesign -s -`): ad-hoc
# подпись пересчитывается заново при каждой пересборке .app, и macOS считает
# это другим приложением — разрешение Accessibility приходится выдавать
# заново после каждой правки кода. Стабильный сертификат даёт одну и ту же
# подпись при каждой сборке, разрешение переживает пересборки.
say "Сертификат для подписи"
if security find-identity -v -p codesigning 2>/dev/null | grep -q "$CERT_NAME"; then
    echo "сертификат «$CERT_NAME» уже есть в Keychain — пропускаю"
else
    echo "сертификата «$CERT_NAME» нет, создаю самоподписанный (только для"
    echo "локальной стабильности подписи, не для распространения приложения)."
    TMP_CERT="$(mktemp -d)"
    trap 'rm -rf "$TMP_CERT"' EXIT
    openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
        -keyout "$TMP_CERT/key.pem" -out "$TMP_CERT/cert.pem" \
        -subj "/CN=$CERT_NAME" \
        -addext "keyUsage=critical,digitalSignature" \
        -addext "extendedKeyUsage=codeSigning" \
        -addext "basicConstraints=critical,CA:false" >/dev/null 2>&1
    openssl pkcs12 -export -out "$TMP_CERT/cert.p12" \
        -inkey "$TMP_CERT/key.pem" -in "$TMP_CERT/cert.pem" -passout pass:lexfix

    echo "Импортирую в Keychain — macOS может спросить пароль от Мака, это"
    echo "ожидаемо (доступ для codesign к приватному ключу)."
    if security import "$TMP_CERT/cert.p12" -k "$HOME/Library/Keychains/login.keychain-db" \
        -P lexfix -T /usr/bin/codesign 2>/dev/null \
        && security add-trusted-cert -d -r trustRoot -p codeSign \
        -k "$HOME/Library/Keychains/login.keychain-db" "$TMP_CERT/cert.pem" 2>/dev/null; then
        echo "готово: сертификат «$CERT_NAME» создан и доверен для code signing"
    else
        echo
        echo "⚠️  Автоматическое создание не прошло (частый случай — Keychain"
        echo "заблокировал импорт без интерактивного подтверждения). Создай"
        echo "сертификат вручную и запусти install.sh снова:"
        echo
        echo "  Keychain Access → меню Keychain Access → Certificate Assistant"
        echo "  → Create a Certificate…"
        echo "    Name: $CERT_NAME"
        echo "    Identity Type: Self Signed Root"
        echo "    Certificate Type: Code Signing"
        echo "  (остальные поля — по умолчанию, Continue → Create → Done)"
        exit 1
    fi
    rm -rf "$TMP_CERT"
    trap - EXIT
fi

# ── 5. LaunchAgent (автозапуск при входе) ─────────────────────────────────
say "LaunchAgent"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LAUNCH_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Applications/LexFix.app/Contents/MacOS/LexFix</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>LANG</key>
        <string>en_US.UTF-8</string>
        <key>LC_ALL</key>
        <string>en_US.UTF-8</string>
    </dict>
    <key>StandardOutPath</key>
    <string>$HOME/Library/Logs/lex-widget.out.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/Library/Logs/lex-widget.err.log</string>
</dict>
</plist>
EOF
echo "готово: $PLIST"

# ── 6. Сборка .app ─────────────────────────────────────────────────────────
# build_app.sh подписывает сертификатом из шага 4, ставит в /Applications,
# перезапускает LaunchAgent.
say "Сборка LexFix.app"
bash build_app.sh

# ── 7. Ollama (опционально — LLM-варианты в попапе) ───────────────────────
say "Ollama (опционально)"
if command -v ollama >/dev/null 2>&1; then
    # Захватываем вывод в переменную, а не льём напрямую в `grep -q`: под
    # pipefail ранний выход grep после первого совпадения шлёт SIGPIPE
    # процессу слева, и пайплайн считается упавшим даже при найденном матче.
    ollama_models="$(ollama list 2>/dev/null || true)"
    if grep -q "qwen2.5:7b" <<< "$ollama_models"; then
        echo "ollama + qwen2.5:7b уже есть — LLM-варианты в попапе доступны"
    else
        echo "Ollama есть, модели qwen2.5:7b нет. Поставить сейчас (~4.7 ГБ)?"
        read -r -p "  [Y/n] " reply
        if [[ ! "$reply" =~ ^[Nn] ]]; then
            ollama pull qwen2.5:7b
        else
            echo "пропущено — можно позже: ollama pull qwen2.5:7b"
        fi
    fi
else
    echo "Ollama не найдена. Без неё LexFix работает (словарные исправления,"
    echo "точные/fuzzy/транслит-совпадения), но не предлагает LLM-варианты"
    echo "написания и не решает омонимы («докер»/Docker) по контексту."
    if command -v brew >/dev/null 2>&1; then
        echo "Поставить: brew install ollama && brew services start ollama"
        echo "           ollama pull qwen2.5:7b"
    fi
fi

say "Готово"
cat <<'EOF'
LexFix установлен и запущен (иконка ✎ в строке меню).

Осталось одно ручное действие — macOS требует его через GUI, автоматизировать
нельзя:

  Системные настройки → Конфиденциальность и безопасность → Универсальный
  доступ → включить LexFix (кнопкой "+", если приложения нет в списке —
  выбери /Applications/LexFix.app).

Без этого разрешения слово, которое ты выделил перед хоткеем, не будет
само появляться в попапе (симуляция Cmd+C для чтения выделения требует
Accessibility). Само приложение и хоткей при этом всё равно работают.

Проверка: bash status.sh
Использование: выдели слово → Cmd+Shift+S → попап с вариантами → Enter.
EOF
