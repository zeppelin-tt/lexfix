"""py2app-сборка LexFix — обёртка над menubar.py в нормальный .app с именем
и иконкой. Собирается обычным (standalone) режимом: бандл несёт внутри свой
Python.framework и PyObjC и ни на что снаружи не ссылается.

Почему НЕ alias-режим (`py2app -A`), хотя раньше был именно он: alias-бандл
физически не может иметь валидную подпись. py2app кладёт в него симлинк
`Contents/MacOS/python` на python из venv, то есть наружу бандла, а codesign
такие симлинки отвергает («invalid destination for symbolic link in bundle»);
плюс оставляет пару висячих симлинков в Resources/lib. Бандл с битой подписью
macOS не сопоставляет с записью в TCC — галочка в «Универсальном доступе»
стоит, а AXIsProcessTrusted() отдаёт False, и CGEventPost молча не работает.
Ровно из-за этого выделенное слово не подставлялось в попап.

Заменить симлинк на копию python нельзя: интерпретатор находит stdlib именно
через разрешение этого симлинка, а копия падает с «No module named
'encodings'» (PYTHONHOME стаб перебивает своим). Отсюда и standalone.

Живой словарь при этом сохраняется. Требование прежнее: corrector.py считает
пути к lexicon.json/learned.json от своего __file__, и виджет обязан править
ТЕ ЖЕ файлы, что `lex.py fix` и сервер. Поэтому настоящий путь до проекта
пишется в Info.plist (`LexFixProjectDir`), а menubar.py при старте ставит его
первым в sys.path — живые corrector.py/lex.py/hotkey.py перекрывают копии,
попавшие в бандл. Копии внутри бандла остаются неиспользованными.

Платите за это тем, что .app привязан к этой машине и к этому пути до
проекта — переносить его на другой Mac бессмысленно, там нужно собирать
заново. Плюс правки самого menubar.py требуют пересборки (build_app.sh),
в отличие от alias-режима.
"""

import os

from setuptools import setup

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

APP = ["menubar.py"]
OPTIONS = {
    "argv_emulation": False,
    "iconfile": "lexfix.icns",
    "plist": {
        "CFBundleName": "LexFix",
        "CFBundleDisplayName": "LexFix",
        "CFBundleIdentifier": "ru.zeppelin.lexfix",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
        "LSUIElement": True,
        "NSHumanReadableCopyright": "",
        # Откуда брать живые corrector.py/lex.py/hotkey.py и их файлы данных.
        # Читается в menubar.py::_project_dir().
        "LexFixProjectDir": PROJECT_DIR,
    },
}

setup(
    app=APP,
    name="LexFix",
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
