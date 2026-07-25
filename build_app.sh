#!/bin/bash
# Пересобирает LexFix.app и ставит в /Applications.
#
# py2app в alias-режиме символлинкует ВСЁ в Resources, включая иконку —
# для .py это то, что нужно (см. setup.py), но для .icns смысла нет, а
# System Settings → «Объекты входа и расширения» показывает системную
# заглушку («exec», «неподтверждённый разработчик») именно для бандлов,
# где ресурс иконки — симлинк наружу пакета. Поэтому здесь иконка
# копируется как обычный файл, а бандл переподписывается заново (иначе
# codesign не совпадёт после ручной правки Resources).
#
# Подписываем локальным сертификатом "LexFix Local Signing" (Keychain Access
# → Certificate Assistant → Code Signing), а не ad-hoc (-s -): ad-hoc подпись
# пересчитывается заново при каждой пересборке, и разрешение Accessibility
# слетает после любой правки. Стабильный сертификат — та же подпись каждый
# раз, разрешение переживает пересборку.
#
# Но только если подпись ВАЛИДНА: TCC сверяет процесс с designated requirement
# («тот же bundle id, тот же сертификат»), а бандл с битой подписью не
# сопоставляется ни с чем — грант в списке есть, толку ноль. Поэтому ниже
# чистятся симлинки и стоит обязательная проверка codesign --verify.
SIGN_ID="LexFix Local Signing"
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x venv/bin/python3 ]; then
    echo "нет venv/ — сначала: bash install.sh (или python3.12 -m venv venv && venv/bin/pip install -r requirements.txt)" >&2
    exit 1
fi

rm -rf build dist
# Standalone, БЕЗ -A: alias-бандл нельзя валидно подписать, а без валидной
# подписи не работает Accessibility (подробности — в setup.py).
venv/bin/python3 setup.py py2app

rm -f dist/LexFix.app/Contents/Resources/lexfix.icns
cp lexfix.icns dist/LexFix.app/Contents/Resources/lexfix.icns

# Висячие симлинки ломают подпись («No such file or directory» на --verify).
# Внутренние симлинки Python.framework (Versions/Current и т.п.) относительные
# и живые — они остаются, codesign их принимает.
find dist/LexFix.app -type l ! -exec test -e {} \; -delete

codesign --force --deep -s "$SIGN_ID" dist/LexFix.app
# Подпись обязана проверяться. Иначе Accessibility отваливается молча, и это
# всплывёт только тем, что в попапе не появится слово. Лучше упасть здесь.
codesign --verify --strict --verbose=2 dist/LexFix.app

launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/local.lex-widget.plist 2>/dev/null || true
rm -rf /Applications/LexFix.app
cp -R dist/LexFix.app /Applications/LexFix.app
/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister -f /Applications/LexFix.app
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.lex-widget.plist

echo "готово: /Applications/LexFix.app переустановлен и перезапущен"
