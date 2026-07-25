#!/usr/bin/env python3
"""Иконка ✎ в строке меню: левый клик — выезжает попап ввода прямо из иконки
(как у Wi-Fi/Bluetooth), правый клик — служебное меню.

Нативный AppKit (PyObjC), один процесс: NSStatusItem + NSPopover. Никакого
tkinter и отдельного окна-«второй программы» — попап рисуется тем же
приложением, что и иконка, и получает родную анимацию выезда/затухания от
самой системы (NSPopover.showRelativeToRect_ofView_preferredEdge_).

Пишет в тот же learned.json, что и `lex.py fix` — вызывает ровно ту же
функцию, логика не дублируется.
"""

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


def _project_dir() -> Path:
    """Каталог lexfix — источник ЖИВЫХ corrector.py, lex.py, hotkey.py.

    В собранном .app считать его от `__file__` нельзя: там `__file__` указывает
    внутрь бандла, и corrector.py вычислил бы пути к learned.json/lexicon.json
    тоже внутри бандла. Виджет правил бы копии, которых ни `lex.py`, ни сервер
    не видят, — словарь молча разъехался бы надвое. hotkey.py по той же причине
    писал бы settings.json внутрь .app, ломая подпись бандла.

    Настоящий путь кладётся в Info.plist при сборке (см. setup.py).
    """
    if getattr(sys, "frozen", None):
        import plistlib
        info = Path(os.environ["RESOURCEPATH"]).parent / "Info.plist"
        try:
            with info.open("rb") as fh:
                project = plistlib.load(fh).get("LexFixProjectDir")
        except (OSError, plistlib.InvalidFileException):
            project = None
        if project and Path(project).is_dir():
            return Path(project)
        # Лучше громко упасть, чем тихо начать править копии словаря в бандле.
        raise RuntimeError(
            "в Info.plist нет валидного LexFixProjectDir — пересобери "
            "виджет через build_app.sh")
    return Path(__file__).resolve().parent


PROJECT_DIR = _project_dir()
sys.path.insert(0, str(PROJECT_DIR))
import hotkey  # noqa: E402
import translit  # noqa: E402
from lex import key_of  # noqa: E402
from corrector import TECH_TERMS_PATH, update_learned  # noqa: E402

log = logging.getLogger("lex-widget")

from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSBox,
    NSBoxSeparator,
    NSButton,
    NSColor,
    NSEventMaskLeftMouseUp,
    NSEventMaskRightMouseUp,
    NSEventTypeRightMouseUp,
    NSFont,
    NSFontWeightSemibold,
    NSMakeRect,
    NSMaxYEdge,
    NSMenu,
    NSMenuItem,
    NSPasteboard,
    NSPasteboardTypeString,
    NSPopover,
    NSPopoverBehaviorTransient,
    NSStatusBar,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSTextAlignmentCenter,
    NSTextAlignmentLeft,
    NSTextAlignmentRight,
    NSTextField,
    NSTextFieldRoundedBezel,
    NSVariableStatusItemLength,
    NSViewController,
    NSVisualEffectView,
    NSVisualEffectMaterialPopover,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectStateActive,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from ApplicationServices import (
    AXIsProcessTrusted,
    AXIsProcessTrustedWithOptions,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateSystemWide,
    AXValueGetValue,
    kAXFocusedApplicationAttribute,
    kAXFocusedUIElementAttribute,
    kAXSelectedTextRangeAttribute,
    kAXTrustedCheckOptionPrompt,
    kAXValueAttribute,
    kAXValueCFRangeType,
)
from Foundation import NSAttributedString, NSMutableAttributedString, NSObject
from PyObjCTools import AppHelper
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventPost,
    CGEventSetFlags,
    CGEventSourceCreate,
    kCGEventFlagMaskCommand,
    kCGEventSourceStateHIDSystemState,
    kCGHIDEventTap,
)

VK_C = 8  # kVK_ANSI_C — физическая клавиша C, для симуляции Cmd+C

# Голое "python3" резолвится через PATH процесса — у launchd/.app он урезан
# до /usr/bin:/bin:/usr/sbin:/sbin и не видит venv вообще. build_lexicon.py
# не использует пакеты venv (только stdlib + translit.py), но интерпретатор
# всё равно должен быть предсказуемым — тем же, что запускает сам виджет.
PYTHON3 = str(PROJECT_DIR / "venv" / "bin" / "python3")
if not Path(PYTHON3).exists():
    log.warning("интерпретатор %s не найден — venv переехал/переименован? "
                "пересборка словаря из виджета работать не будет", PYTHON3)

WIDTH = 300
PAD = 14
FULL_W = WIDTH - 2 * PAD

# Пара полей «было → стало» с узкой колонкой под стрелку между ними.
ARROW_W = 22
FIELD_W = (FULL_W - ARROW_W) // 2
X_LEFT = PAD
X_ARROW = X_LEFT + FIELD_W
X_RIGHT = X_ARROW + ARROW_W

HINT_W = 62
H_HEADER, H_FIELD, H_STATUS = 14, 24, 13

# Кнопок нет намеренно: обе секции применяются по Enter. Одна кнопка на две
# секции читалась как общая (стояла между блоками и выглядела применимой к
# любому, хотя применяла только верхний), а две кнопки — это два лишних ряда
# ради действия, которое и так делается клавишей. Вместо них подсказка «Enter»
# в строке заголовка: она объясняет, что делать с полем, и не занимает высоту.
VAR_H = 20        # высота одной строки списка вариантов
MAX_VARIANTS = 6  # больше в попап по-человечески не влезает


def _layout(n_variants: int) -> tuple[int, dict[str, int]]:
    """Высота попапа и Y-координаты рядов для заданного числа вариантов.

    Раскладка описывается списком рядов, а не россыпью Y-констант: раньше
    координаты считались руками и разъезжались (кнопка «Применить» наползала
    на поле). Здесь высота выводится из содержимого, а вставка ряда не требует
    пересчитывать всё, что ниже.

    Функция, а не константы — потому что список вариантов появляется и
    исчезает: без подсказок попап остаётся компактным, с ними подрастает
    ровно на нужное. Ряд «variants» вставляется между полями и разделителем.
    """
    rows = [
        # имя,      высота,             отступ снизу
        ("header1", H_HEADER,           4),
        ("fields",  H_FIELD,            6 if n_variants else 14),
    ]
    if n_variants:
        rows.append(("variants", n_variants * VAR_H, 12))
    rows += [
        ("sep",     1,                  14),
        ("header2", H_HEADER,           4),
        ("field2",  H_FIELD,            10),
        ("status",  H_STATUS,           0),
    ]
    height = 2 * PAD + sum(h for _, h, _ in rows) + sum(g for _, _, g in rows)
    y: dict[str, int] = {}
    cursor = height - PAD
    for name, h, gap in rows:
        cursor -= h
        y[name] = cursor
        cursor -= gap
    return height, y


HEIGHT, Y = _layout(0)


def apply_fix(wrong: str, right: str) -> str | None:
    """Та же логика, что `lex.py fix` — единый источник правды."""
    wrong, right = wrong.strip(), right.strip()
    if not wrong or not right:
        return "заполни оба поля"
    key = key_of(wrong)
    if not key:
        return f"«{wrong}» — не слово, правило не построить"
    update_learned(lambda data: {**data, key: right})
    return None


def _text(text: str, x: float, y: float, w: float, h: float,
          size: float, color, font=None) -> NSTextField:
    lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
    lbl.setStringValue_(text)
    lbl.setBezeled_(False)
    lbl.setDrawsBackground_(False)
    lbl.setEditable_(False)
    lbl.setSelectable_(False)
    lbl.setFont_(font or NSFont.systemFontOfSize_(size))
    lbl.setTextColor_(color)
    return lbl


def _header(text: str, y: float) -> NSTextField:
    """Заголовок секции — как в Системных настройках: мелкий, полужирный, серый."""
    return _text(text, PAD, y, FULL_W - HINT_W, H_HEADER, 11,
                 NSColor.secondaryLabelColor(),
                 NSFont.systemFontOfSize_weight_(11, NSFontWeightSemibold))


def _hint(y: float) -> NSTextField:
    """«Enter» в правом краю строки заголовка — единственный способ применить."""
    h = _text("Enter ↵", WIDTH - PAD - HINT_W, y, HINT_W, H_HEADER, 11,
              NSColor.tertiaryLabelColor())
    h.setAlignment_(NSTextAlignmentRight)
    return h


def _variant_title(text: str, source: str, selected: bool):
    """Строка списка: сам вариант и мелким серым — откуда он взялся.

    Источник показывается не из любопытства: «правило» и «словарь» — это
    проверенные имена, а «догадка» — выдумка нейросети, и доверие к ним
    разное. Без пометки они выглядели бы одинаково весомо.
    """
    main = (NSColor.alternateSelectedControlTextColor() if selected
            else NSColor.labelColor())
    sub = (NSColor.alternateSelectedControlTextColor() if selected
           else NSColor.tertiaryLabelColor())
    title = NSMutableAttributedString.alloc().init()
    title.appendAttributedString_(
        NSAttributedString.alloc().initWithString_attributes_(
            "  " + text, {NSForegroundColorAttributeName: main,
                          NSFontAttributeName: NSFont.systemFontOfSize_(12)}))
    title.appendAttributedString_(
        NSAttributedString.alloc().initWithString_attributes_(
            "   " + source, {NSForegroundColorAttributeName: sub,
                             NSFontAttributeName: NSFont.systemFontOfSize_(10)}))
    return title


# Задержка перед симуляцией Cmd+C, в миллисекундах. Живёт как модульная
# переменная, а не константа: значение приходит из settings.json при старте
# (см. applicationDidFinishLaunching_) и меняется на лету при сохранении в
# окне настроек — рестарт виджета для этого не нужен.
#
# Обнаружено эмпирически: с физической клавиатурой Cmd+C всегда доходил без
# паузы (дефолт 0 мс), а через хоткей, назначенный кнопкой в Logitech
# Options+, буфер иногда не менялся вовсе — при этом фронтальное приложение
# и состояние клавиш-модификаторов в момент отправки были ИДЕНТИЧНЫ рабочему
# случаю (см. историю в git). Похоже на гонку в очереди событий ОС: стороннее
# ПО, видимо, эмулирует комбинацию не так, как настоящая клавиатура, и наш
# Cmd+C иногда улетает раньше, чем система доразгребёт его собственную
# последовательность. 50 мс убирало проблему стабильно в этом случае — но это
# не доказанная причина, а дешёвая практическая мера, и не всем нужна, поэтому
# дефолт для новых профилей — 0.
_copy_delay_ms = hotkey.DEFAULT_COPY_DELAY_MS


def _wait_before_copy() -> None:
    if _copy_delay_ms:
        time.sleep(_copy_delay_ms / 1000.0)


def _post_copy_keystroke() -> tuple[bool, int]:
    """Симулирует Cmd+C во фронтовом приложении — тому, что было активно ДО
    открытия попапа. Порядок критичен: если сперва активировать наше
    accessory-приложение, Cmd+C уйдёт нам, а не туда, где реально выделен
    текст.

    Возвращает `(trusted, change_count)`:

    * `trusted` — есть ли грант Accessibility. False означает «точно не
      сработает», ждать смены буфера незачем. True НЕ гарантирует успеха:
      грант может числиться, а событие всё равно отбрасываться (например,
      подпись .app невалидна) — поэтому дальше всё равно ждём реальной смены
      буфера (_watch_clipboard_change).
    * `change_count` — снимок NSPasteboard.changeCount() **до** отправки
      события. Снимать его после нельзя: CGEventPost асинхронный, но целевое
      приложение вполне может успеть скопировать раньше, чем мы дойдём до
      следующей строки, — тогда сравнивать будет не с чем, changed навсегда
      останется False, и виджет покажет ложное «не удалось забрать выделение»
      как раз в тот момент, когда всё сработало.
    """
    before = NSPasteboard.generalPasteboard().changeCount()
    trusted = AXIsProcessTrusted()
    if not trusted:
        log.warning("Accessibility не разрешена — Cmd+C не симулируется")

    src = CGEventSourceCreate(kCGEventSourceStateHIDSystemState)
    down = CGEventCreateKeyboardEvent(src, VK_C, True)
    up = CGEventCreateKeyboardEvent(src, VK_C, False)
    CGEventSetFlags(down, kCGEventFlagMaskCommand)
    CGEventSetFlags(up, kCGEventFlagMaskCommand)
    CGEventPost(kCGHIDEventTap, down)
    CGEventPost(kCGHIDEventTap, up)
    return trusted, before


def _watch_clipboard_change(before_count: int, callback) -> None:
    """Ждёт до 250мс, пока буфер не сменит changeCount с `before_count`, и
    зовёт `callback(changed: bool)` в главном потоке.

    `before_count` приходит из _post_copy_keystroke(), снятый ДО отправки
    события — почему именно так, см. там же.
    """
    pb = NSPasteboard.generalPasteboard()

    def worker():
        deadline = time.monotonic() + 0.25
        changed = False
        while time.monotonic() < deadline:
            if pb.changeCount() != before_count:
                changed = True
                break
            time.sleep(0.01)
        AppHelper.callAfter(callback, changed)

    threading.Thread(target=worker, daemon=True).start()


CONTEXT_WORDS = 3           # слов с каждой стороны выделения — просим у Accessibility
CONTEXT_WINDOW_CHARS = 200  # берём с запасом до разбиения на слова, не весь документ


def _read_selection_context(n_words: int = CONTEXT_WORDS) -> str | None:
    """Слова вокруг текущего выделения в активном приложении — через
    Accessibility API, без похода в буфер обмена (тот отдаёт только само
    выделение, не то, что вокруг). Звать до активации своего процесса —
    иначе «активным приложением» окажемся уже мы сами, и все запросы уйдут
    в наш собственный, пустой UI.

    Работает не везде: многие веб-страницы в браузере и часть Electron-
    приложений не реализуют kAXValue/kAXSelectedTextRange для текстовых
    полей. Любая неудача — тихо None, обычный путь (просто буфер обмена,
    без контекста) продолжает работать как раньше.
    """
    try:
        system = AXUIElementCreateSystemWide()
        err, app = AXUIElementCopyAttributeValue(
            system, kAXFocusedApplicationAttribute, None)
        if err or app is None:
            return None
        err, element = AXUIElementCopyAttributeValue(
            app, kAXFocusedUIElementAttribute, None)
        if err or element is None:
            return None
        err, full_text = AXUIElementCopyAttributeValue(
            element, kAXValueAttribute, None)
        if err or not isinstance(full_text, str) or not full_text:
            return None
        err, range_value = AXUIElementCopyAttributeValue(
            element, kAXSelectedTextRangeAttribute, None)
        if err or range_value is None:
            return None
        ok, rng = AXValueGetValue(range_value, kAXValueCFRangeType, None)
        if not ok:
            return None
        loc, length = int(rng[0]), int(rng[1])
        if loc < 0 or loc > len(full_text) or length < 0:
            return None
        end = min(loc + length, len(full_text))

        # Локальное окно вокруг выделения, а не весь документ целиком — в
        # веб-редакторах kAXValue может отдать содержимое на десятки тысяч
        # символов, и разбивать его на слова только ради трёх с каждой
        # стороны незачем.
        before = full_text[max(0, loc - CONTEXT_WINDOW_CHARS):loc]
        after = full_text[end:end + CONTEXT_WINDOW_CHARS]
        selected = full_text[loc:end].strip()

        before_words = before.split()[-n_words:]
        after_words = after.split()[:n_words]
        if not before_words and not after_words:
            return None  # выделена вся фраза целиком — окружения нет

        parts = before_words + ([selected] if selected else []) + after_words
        return " ".join(parts).strip() or None
    except Exception:  # noqa: BLE001 — контекст опционален, виджет не должен падать
        log.exception("не смог прочитать контекст через Accessibility")
        return None


def _field(x: float, y: float, w: float, placeholder: str) -> NSTextField:
    f = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, H_FIELD))
    f.setFont_(NSFont.systemFontOfSize_(12))
    f.setBezelStyle_(NSTextFieldRoundedBezel)
    f.setPlaceholderString_(placeholder)
    return f


class _HotkeyField(NSTextField):
    """Поле-«рекордер»: клик переводит его в режим записи, следующая
    нажатая комбинация клавиш становится новым хоткеем.

    `performKeyEquivalent_` перехватывает Cmd-комбинации раньше обычного
    keyDown — так же работают open-source shortcut recorder'ы (MASShortcut,
    KeyHolder). Пока `_recording` не выставлен явным кликом, метод
    возвращает NO и не мешает обычным Cmd-комбинациям окна (например Cmd+W).
    """

    def setup_(self, target) -> None:
        self._target = target
        self._recording = False
        self.setEditable_(False)
        self.setSelectable_(False)
        self.setBezeled_(True)
        self.setBezelStyle_(NSTextFieldRoundedBezel)
        self.setAlignment_(NSTextAlignmentCenter)
        self.setFont_(NSFont.systemFontOfSize_(13))

    def acceptsFirstResponder(self):
        return True

    def mouseDown_(self, _event):
        self._recording = True
        self.setStringValue_("Нажмите комбинацию…")
        window = self.window()
        if window is not None:
            window.makeFirstResponder_(self)

    def performKeyEquivalent_(self, event) -> bool:
        if not self._recording:
            return False
        modifiers = hotkey.carbon_mods_from_ns(event.modifierFlags())
        if modifiers == 0:
            return True  # голая клавиша без модификатора — ждём ещё
        keycode = event.keyCode()
        self._recording = False
        self.setStringValue_(hotkey.describe(keycode, modifiers))
        if self._target is not None:
            self._target.hotkeyRecorded_modifiers_(keycode, modifiers)
        return True


class AppDelegate(NSObject):
    def applicationDidFinishLaunching_(self, _notification):
        self._corrector_cache = None
        self._suggestion_generation = 0
        self._popover_generation = 0  # см. _open_popover/_on_clipboard_copied
        self._prefilled = ("", "", "")  # что подставили из буфера — см. _prefill_and_remember
        self._context = None  # слова вокруг выделения — см. _read_selection_context
        self.settings_window = None
        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        button = self.status_item.button()
        button.setTitle_("✎")
        button.setTarget_(self)
        button.setAction_("statusItemClicked:")
        button.sendActionOn_(NSEventMaskLeftMouseUp | NSEventMaskRightMouseUp)

        self._build_popover()
        self._build_menu()
        self._build_edit_menu()

        keycode, modifiers = hotkey.load()
        if not hotkey.register(self.hotkeyPressed, keycode, modifiers):
            log.warning("не удалось занять хоткей %s — комбинация уже занята",
                        hotkey.describe(keycode, modifiers))

        global _copy_delay_ms
        _copy_delay_ms = hotkey.load_copy_delay_ms()

        # Просим Accessibility явно при старте — иначе симуляция Cmd+C в
        # _post_copy_keystroke() молча ничего не делает, и это неотличимо от
        # «ничего не выделено». После пересборки .app запись в «Универсальном
        # доступе», сделанная для прежней подписи, остаётся мёртвой — её нужно
        # удалить кнопкой «−» и добавить .app заново; система покажет запрос.
        AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})

    # --- содержимое попапа ---------------------------------------------

    def _build_popover(self):
        # Настоящий фон, как у системных попапов (Wi-Fi/Bluetooth) — без него
        # контролы рисуются на прозрачном фоне и просвечивает рабочий стол.
        view = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, HEIGHT))
        view.setMaterial_(NSVisualEffectMaterialPopover)
        view.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        view.setState_(NSVisualEffectStateActive)

        # Реестр «вьюха → в каком ряду стоит»: нужен, чтобы _relayout_rows() мог
        # переставить всё разом, когда попап растёт под список вариантов.
        self._placed: list = []

        def place(v, row, x, w, h, dy=0):
            v.setFrame_(NSMakeRect(x, Y[row] + dy, w, h))
            self._placed.append((v, row, x, w, h, dy))
            view.addSubview_(v)
            return v

        # --- секция 1: жёсткое правило «слышится → пишем» ---
        place(_header("Исправить ошибку", Y["header1"]), "header1",
              PAD, FULL_W - HINT_W, H_HEADER)
        place(_hint(Y["header1"]), "header1", WIDTH - PAD - HINT_W, HINT_W, H_HEADER)

        self.field_wrong = _field(X_LEFT, Y["fields"], FIELD_W, "спокинги")
        self.field_right = _field(X_RIGHT, Y["fields"], FIELD_W, "Spokenly")
        for f, x in ((self.field_wrong, X_LEFT), (self.field_right, X_RIGHT)):
            f.setDelegate_(self)
            place(f, "fields", x, FIELD_W, H_FIELD)

        arrow = _text("→", X_ARROW, Y["fields"] + 3, ARROW_W, 18, 13,
                      NSColor.tertiaryLabelColor())
        arrow.setAlignment_(NSTextAlignmentCenter)
        place(arrow, "fields", X_ARROW, ARROW_W, 18, 3)

        # Строки вариантов создаются сразу все и прячутся: показать/спрятать
        # дешевле и спокойнее, чем добавлять и удалять subview на каждый ответ
        # нейросети.
        self.variants: list[tuple[str, str]] = []
        self.variant_index = -1
        self.variant_buttons = []
        for i in range(MAX_VARIANTS):
            b = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, 0, FULL_W, VAR_H))
            b.setBordered_(False)
            b.setAlignment_(NSTextAlignmentLeft)
            b.setTarget_(self)
            b.setAction_("variantClicked:")
            b.setTag_(i)
            b.setWantsLayer_(True)
            b.layer().setCornerRadius_(4.0)
            b.setHidden_(True)
            view.addSubview_(b)
            self.variant_buttons.append(b)

        sep = NSBox.alloc().initWithFrame_(NSMakeRect(PAD, Y["sep"], FULL_W, 1))
        sep.setBoxType_(NSBoxSeparator)
        place(sep, "sep", PAD, FULL_W, 1)

        # --- секция 2: выучить имя, решение оставить контексту ---
        place(_header("Выучить имя", Y["header2"]), "header2",
              PAD, FULL_W - HINT_W, H_HEADER)
        place(_hint(Y["header2"]), "header2", WIDTH - PAD - HINT_W, HINT_W, H_HEADER)
        self.field_name = _field(PAD, Y["field2"], FULL_W, "Flutter, Vermona, Docker…")
        self.field_name.setDelegate_(self)
        place(self.field_name, "field2", PAD, FULL_W, H_FIELD)

        self.status_label = _text("", PAD, Y["status"], FULL_W, H_STATUS, 11,
                                  NSColor.secondaryLabelColor())
        place(self.status_label, "status", PAD, FULL_W, H_STATUS)

        self.popover_view_controller = NSViewController.alloc().init()
        self.popover_view_controller.setView_(view)

        self.popover = NSPopover.alloc().init()
        self.popover.setContentViewController_(self.popover_view_controller)
        self.popover.setContentSize_((WIDTH, HEIGHT))
        self.popover.setBehavior_(NSPopoverBehaviorTransient)  # закрывается кликом мимо
        self.popover.setAnimates_(True)

    def _build_edit_menu(self):
        """Без этого не работают Cmd+C/Cmd+V в полях.

        Стандартные Cut/Copy/Paste в AppKit — не встроенное поведение поля,
        а пункты меню «Правка» с клавиатурными эквивалентами: нажатие ловит
        главное меню и рассылает `paste:` по цепочке респондеров. У accessory-
        приложения главного меню нет вообще, поэтому Cmd+V просто некому было
        обработать. Меню на экране не показывается (иконка в статус-баре, а не
        в Dock) — оно нужно ровно ради этих эквивалентов.
        """
        main = NSMenu.alloc().init()
        item = NSMenuItem.alloc().init()
        main.addItem_(item)

        edit = NSMenu.alloc().initWithTitle_("Правка")
        for title, action, key in (
            ("Отменить", "undo:", "z"),
            ("Вернуть", "redo:", "Z"),
            ("Вырезать", "cut:", "x"),
            ("Копировать", "copy:", "c"),
            ("Вставить", "paste:", "v"),
            ("Выбрать всё", "selectAll:", "a"),
        ):
            edit.addItemWithTitle_action_keyEquivalent_(title, action, key)
        item.setSubmenu_(edit)
        NSApplication.sharedApplication().setMainMenu_(main)

    def _build_menu(self):
        self.menu = NSMenu.alloc().init()
        for title, action in (
            ("Открыть learned.json", "openLearned:"),
            ("Пересобрать словарь", "rebuildLexicon:"),
            ("Настройки…", "openSettings:"),
        ):
            item = self.menu.addItemWithTitle_action_keyEquivalent_(title, action, "")
            item.setTarget_(self)
        self.menu.addItem_(NSMenuItem.separatorItem())
        quit_item = self.menu.addItemWithTitle_action_keyEquivalent_("Выход", "terminate:", "q")
        quit_item.setTarget_(NSApplication.sharedApplication())

    def _build_settings_window(self):
        rect = NSMakeRect(0, 0, 300, 216)
        self.settings_window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False,
        )
        self.settings_window.setTitle_("Настройки")
        self.settings_window.setReleasedWhenClosed_(False)
        content = self.settings_window.contentView()

        label = _text("Хоткей для попапа — клик по полю, затем комбинация:",
                      16, 170, 268, 30, 11, NSColor.secondaryLabelColor())
        content.addSubview_(label)

        self.hotkey_field = _HotkeyField.alloc().initWithFrame_(NSMakeRect(16, 136, 268, 26))
        self.hotkey_field.setup_(self)
        content.addSubview_(self.hotkey_field)

        delay_label = _text(
            "Задержка перед копированием, мс — 0 обычно ок, ~50 для "
            "Logitech Options+:",
            16, 92, 268, 30, 11, NSColor.secondaryLabelColor())
        content.addSubview_(delay_label)

        # Без setDelegate_(self): тот же делегат, что у полей попапа, ловит
        # Enter как «применить правило коррекции» (control_textView_do
        # CommandBySelector_) — здесь это просто число, чужая логика ни к
        # чему, а из-за общего делегата Enter тут дёрнул бы _apply_rule().
        self.delay_field = _field(16, 58, 268, "0")
        content.addSubview_(self.delay_field)

        save_btn = NSButton.alloc().initWithFrame_(NSMakeRect(16, 16, 268, 28))
        save_btn.setTitle_("Сохранить")
        save_btn.setBezelStyle_(NSBezelStyleRounded)
        save_btn.setTarget_(self)
        save_btn.setAction_("saveSettings:")
        content.addSubview_(save_btn)

        self._refresh_settings_window()

    def _refresh_settings_window(self):
        keycode, modifiers = hotkey.load()
        self._pending_hotkey = (keycode, modifiers)
        self.hotkey_field.setStringValue_(hotkey.describe(keycode, modifiers))
        self.delay_field.setStringValue_(str(hotkey.load_copy_delay_ms()))

    def hotkeyRecorded_modifiers_(self, keycode, modifiers):
        self._pending_hotkey = (keycode, modifiers)

    def saveSettings_(self, _sender):
        keycode, modifiers = self._pending_hotkey
        if not hotkey.register(self.hotkeyPressed, keycode, modifiers):
            self.hotkey_field.setStringValue_(
                f"{hotkey.describe(keycode, modifiers)} — занято другим приложением")
            return
        hotkey.save(keycode, modifiers)

        # Невалидный ввод (пусто, буквы, отрицательное) молча трактуем как
        # «оставить как было» — это настройка на случай проблемы, а не то,
        # где стоит блокировать сохранение хоткея из-за опечатки в цифре.
        try:
            ms = max(0, int(self.delay_field.stringValue().strip()))
        except ValueError:
            ms = hotkey.load_copy_delay_ms()
        hotkey.save_copy_delay_ms(ms)
        global _copy_delay_ms
        _copy_delay_ms = ms

        self.settings_window.performClose_(None)

    def openSettings_(self, _sender):
        if self.settings_window is None:
            self._build_settings_window()
        else:
            self._refresh_settings_window()
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.settings_window.makeKeyAndOrderFront_(None)

    # --- клики по иконке --------------------------------------------------

    def statusItemClicked_(self, _sender):
        event = NSApplication.sharedApplication().currentEvent()
        if event.type() == NSEventTypeRightMouseUp:
            self._show_menu()
        else:
            self._toggle_popover()

    def _show_menu(self):
        # Левый клик обрабатывается отдельно (см. sendActionOn_ выше), поэтому
        # меню на statusItem не висит постоянно — иначе оно перехватило бы
        # и левый клик тоже. Показываем временно, только на правый клик.
        button = self.status_item.button()
        self.status_item.setMenu_(self.menu)
        button.performClick_(None)
        self.status_item.setMenu_(None)

    def hotkeyPressed(self):
        """Хоткей (по умолчанию Cmd+Shift+S) — открыть попап поверх активного
        приложения. Забираем выделение ДО активации своего процесса — иначе
        Cmd+C улетит уже нам, а не туда, где реально что-то выделено."""
        if self.popover.isShown():
            self.popover.performClose_(None)
            return
        _wait_before_copy()
        trusted, before = _post_copy_keystroke()
        context = _read_selection_context()
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self._open_popover(trusted=trusted, before_count=before, context=context)

    def _toggle_popover(self):
        if self.popover.isShown():
            self.popover.performClose_(None)
            return
        _wait_before_copy()
        trusted, before = _post_copy_keystroke()
        context = _read_selection_context()
        self._open_popover(trusted=trusted, before_count=before, context=context)

    def _open_popover(self, trusted: bool = True, before_count: int = -1,
                      context: str | None = None):
        # Новое открытие — новое поколение: без этого фоновый поток,
        # ждущий смену буфера от ПРЕДЫДУЩЕГО нажатия (см. _on_clipboard_copied),
        # ничем не отличал бы «это открытие ещё актуально» от «уже открыли
        # заново» и мог дозаполнить текущий попап словом из прошлого нажатия,
        # если предыдущая проверка буфера досчитывает уже после того, как
        # новый попап открылся (обнаружено живьём при быстрых повторных
        # нажатиях хоткея).
        self._popover_generation += 1
        generation = self._popover_generation
        # Снимок контекста делается ДО активации своего процесса (см.
        # hotkeyPressed/_toggle_popover) — здесь только сохраняем его на всё
        # время жизни попапа, читать заново уже поздно: фокус ушёл нам.
        self._context = context
        # Показываем сразу с тем, что уже лежит в буфере, и не ждём симуляцию
        # Cmd+C. Ждать нельзя по двум причинам: попап подвисал бы на четверть
        # секунды на каждый хоткей, а главное — ручное копирование (выделил,
        # Cmd+C, потом хоткей) вообще не требует Accessibility и работало бы
        # даже без гранта, но с пустым полем и пугающим предупреждением.
        # Если за 250мс придёт что-то свежее — поля перезаполнит
        # _on_clipboard_copied.
        button = self.status_item.button()
        # Список от прошлого открытия относился к другому слову — убираем до
        # показа, иначе попап на мгновение мелькнёт чужими вариантами.
        # animated=False: попап ещё не показан, анимировать нечего.
        self._clear_variants(animated=False)
        self.popover.showRelativeToRect_ofView_preferredEdge_(
            button.bounds(), button, NSMaxYEdge
        )
        focus = self._prefill_and_remember()
        window = self.popover_view_controller.view().window()
        if window is not None:
            window.makeFirstResponder_(focus)
        if trusted:
            _watch_clipboard_change(
                before_count,
                lambda changed: self._on_clipboard_copied(generation, changed))
        else:
            # Гранта точно нет — ждать смены буфера незачем, но и ругаться
            # незачем, если из буфера уже что-то подставилось.
            self._warn_no_selection(
                "Accessibility не разрешена — выделение не забирается "
                "автоматически",
                "⚠ Нет доступа к выделению: Системные настройки → "
                "Конфиденциальность и безопасность → Универсальный доступ → "
                "LexFix. Или скопируй слово руками (Cmd+C).")

    def _field_snapshot(self) -> tuple:
        return (str(self.field_wrong.stringValue()),
                str(self.field_right.stringValue()),
                str(self.field_name.stringValue()))

    def _prefill_and_remember(self) -> NSTextField:
        """Подставляет слово из буфера и запоминает, что именно подставили.

        Снимок нужен, чтобы отличить «поле заполнили мы» от «юзер успел
        вписать сам»: без него повторная подстановка (когда долетел Cmd+C)
        видела бы собственный же прежний текст и считала поле занятым.
        """
        focus = self._prefill_from_clipboard()
        self._prefilled = self._field_snapshot()
        return focus

    def _warn_no_selection(self, log_message: str, hint: str) -> None:
        """Говорит в статус-строку, только если подставить было нечего — иначе
        слово в поле уже есть (скопировали руками), и предупреждение лишь
        пугает."""
        log.info(log_message)
        if any(v.strip() for v in self._prefilled):
            return
        self.status_label.setStringValue_(hint)

    def _on_clipboard_copied(self, generation: int, changed: bool) -> None:
        """Реакция на результат симуляции Cmd+C.

        changed=True — буфер сменился, выделение скопировалось: перезаполняем
        поля свежим словом (если юзер не начал править их сам).
        changed=False — буфер не сменился. Это НЕ ошибка: чаще всего просто
        ничего не было выделено (попап открыли, чтобы вписать слово руками),
        и приложение проигнорировало Cmd+C. Реже — сломана подпись .app и TCC
        не сопоставляет процесс с грантом, но это ловится `bash status.sh`,
        а не догадками здесь, поэтому в UI про подпись молчим.

        `generation` — снимок _popover_generation на момент ЭТОГО открытия
        (см. _open_popover). Проверка ниже отсекает случай, пойманный живьём:
        быстрое повторное нажатие хоткея закрывает и открывает попап заново
        раньше, чем 250мс-ожидание буфера от ПРЕДЫДУЩЕГО нажатия успевает
        досчитать — без проверки поколения этот поздний колбэк молча
        дозаполнял бы уже другой, новый попап словом из прошлого нажатия.
        """
        if generation != self._popover_generation:
            return
        if self.popover is None or not self.popover.isShown():
            return
        if not changed:
            self._warn_no_selection(
                "буфер не сменился за 250мс — вероятно, ничего не было выделено",
                "Ничего не выделено. Впиши слово руками — или выдели его "
                "и открой попап заново.")
            return
        # Юзер уже правил поля сам — его ввод важнее свежего буфера.
        if self._field_snapshot() != self._prefilled:
            return
        # Чистим перед повторной подстановкой: новое слово может уехать в
        # другое поле (омоним — в нижнее), и старое значение осталось бы висеть.
        self.field_wrong.setStringValue_("")
        self.field_right.setStringValue_("")
        self.field_name.setStringValue_("")
        # Варианты подбирались под прежнее слово из буфера — они больше не о нём.
        self._clear_variants()
        focus = self._prefill_and_remember()
        window = self.popover_view_controller.view().window()
        if window is not None:
            window.makeFirstResponder_(focus)

    # --- подстановка из буфера --------------------------------------------

    def _prefill_from_clipboard(self) -> NSTextField:
        """Слово из буфера сразу в поле — открыл попап и правь, без Cmd+V.

        Омоним («флаттер», «докер») уезжает в нижнюю секцию: жёсткое правило
        ему противопоказано — оно намертво прибьёт одно из двух значений,
        а нужно, чтобы каждый раз решал контекст. В нижнее поле подставляется
        уже разрешённое английское имя, а не само кириллическое слово: там
        учится имя, а «флаттер» именем не является.
        """
        board = NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString)
        word = (str(board) if board else "").strip()
        if not word or len(word.split()) > 3 or "\n" in word:
            return self.field_wrong

        name = self._homonym_name(word)
        if name:
            self.field_name.setStringValue_(name)
            self.status_label.setStringValue_(
                f"«{word}» — омоним, решает контекст. Ниже: выучить имя")
            return self.field_name

        self.field_wrong.setStringValue_(word)
        self._request_suggestion(word, self._context)
        return self.field_wrong

    def _homonym_name(self, word: str) -> str | None:
        """Если слово — известный омоним, вернуть его английское написание."""
        if not translit.has_cyrillic(word) or " " in word:
            return None
        corrector = self._corrector()
        if corrector is None or word.lower() not in corrector.homonyms:
            return None
        keys = corrector.by_skeleton.get(translit.skeleton(word), [])
        if not keys:
            return None
        best = max(keys, key=lambda k: corrector.lexicon[k]["weight"])
        return corrector.lexicon[best]["name"]

    def _corrector(self):
        """Словарь грузится лениво: он не нужен, пока попап не открыли.

        llm_enabled=True даже для синхронного пути (распознавание омонимов):
        сам по себе он LLM не дёргает — только `suggest_variants()` из потока
        это делает, а флаг общий на весь экземпляр корректора.
        """
        if self._corrector_cache is None:
            try:
                from corrector import Corrector
                self._corrector_cache = Corrector(llm_enabled=True)
            except Exception:  # noqa: BLE001 — без словаря виджет всё равно рабочий
                log.exception("не смог загрузить словарь, омонимы не распознаю")
                return None
        else:
            self._corrector_cache._reload_if_changed()
        return self._corrector_cache

    # --- авто-предложение от LLM -------------------------------------------

    def _request_suggestion(self, word: str, context: str | None = None) -> None:
        """Нейросетка подбирает варианты замены, пока пользователь смотрит на
        попап: похожие имена из словаря и свои догадки про опечатку или
        транслитерацию (см. `corrector.suggest_variants`).

        `context` — слова вокруг выделения, добытые через Accessibility API
        (см. `_read_selection_context`) в момент открытия попапа, ДО того как
        мы забрали фокус себе. Без него виджету известно только само слово,
        и догадки заметно слабее, чем во время транскрипции, где LLM видит
        фразу целиком — с контекстом это подтягивается ближе к тому уровню.
        Список в любом случае черновик: лучший вариант подставляется в поле
        сразу, чтобы частый случай «угадала с первого раза» закрывался одним
        Enter, а остальные ждут ↑↓ или клика.
        """
        self._suggestion_generation += 1
        generation = self._suggestion_generation
        self.status_label.setStringValue_("прошу нейросеть…")

        def worker():
            corrector = self._corrector()
            try:
                variants = (corrector.suggest_variants(word, context=context or "")
                           if corrector else [])
            except Exception:  # noqa: BLE001 — виджет не должен падать из-за сети/Ollama
                log.exception("не смог получить варианты от нейросети")
                variants = []
            AppHelper.callAfter(self._apply_variants, generation, word, variants)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_variants(self, generation: int, word: str, variants: list) -> None:
        if generation != self._suggestion_generation:
            return  # попап успели закрыть/открыть заново — ответ устарел
        if str(self.field_wrong.stringValue()) != word:
            return  # «как слышится» уже правили руками
        self.variants = list(variants)[:MAX_VARIANTS]
        self._relayout_rows(len(self.variants))
        if not self.variants:
            self.status_label.setStringValue_("нейросеть не нашла замену — впиши руками")
            return
        if not str(self.field_right.stringValue()):
            self.variant_index = 0
            self.field_right.setStringValue_(self.variants[0][0])
            # Подстановка — это по-прежнему «заполнили мы», а не правка юзера.
            # Без обновления снимка _on_clipboard_copied решит, что человек уже
            # печатает, и не заменит устаревшее слово из буфера на свежее,
            # только что скопированное хоткеем. Словарные попадания приходят
            # за миллисекунды — почти всегда раньше, чем долетит Cmd+C.
            self._prefilled = self._field_snapshot()
        self._paint_variants()
        self.status_label.setStringValue_(
            f"вариантов: {len(self.variants)} — ↑↓ выбрать, Enter применить")

    def _clear_variants(self, animated: bool = True) -> None:
        self.variants = []
        self.variant_index = -1
        self._relayout_rows(0, animated=animated)

    def _relayout_rows(self, n: int, animated: bool = True) -> None:
        """Переставляет попап под `n` строк вариантов.

        Все вьюхи спозиционированы абсолютно, а начало координат у AppKit —
        снизу слева, поэтому смена высоты сдвигает вообще всё. Отсюда реестр
        `_placed`: пройтись по нему дешевле и надёжнее, чем держать
        autoresizing-маски на два десятка контролов.

        `animated` временно ничего не делает — тут стояла анимация выезжания
        вариантов (плавный resize попапа + fade-in строк через
        NSAnimationContext), но после неё перестало подтягиваться выделенное
        слово при быстрых повторных нажатиях хоткея. Не доказано, что дело
        именно в этом коде, а не в совпадении по времени, — отключено для
        проверки гипотезы, сам код анимации в git не сохранён (откатили до
        первого коммита этого файла). Вероятная причина, если будешь
        восстанавливать: `header1`/`fields` (поля слова) тоже входят в
        `_placed` и потому тоже двигались `.animator().setFrame_()` при
        каждом изменении числа вариантов — хотя визуально двигаться должны
        только сами строки вариантов. Первый кандидат на фикс — исключить
        `header1`/`fields` из анимируемого прохода по `_placed`, оставив
        анимацию только `variant_buttons`. Параметр оставлен, чтобы не
        трогать вызовы `_clear_variants`.
        """
        height, y = _layout(n)
        self.popover.setContentSize_((WIDTH, height))
        # Origin content-view'а трогать нельзя — его ставит сам NSPopover, и
        # там не ноль: окно попапа больше контента на рамку со стрелкой (13pt
        # с каждой стороны), поэтому вьюха живёт со смещением (13, 13).
        # Обнуление origin сдвигало всё содержимое на эти 13pt влево и вниз —
        # попап выглядел «съехавшим», а нижняя строка статуса обрезалась.
        # Меняем только размер, оставляя смещение таким, каким его назначила
        # система.
        view = self.popover_view_controller.view()
        origin = view.frame().origin
        view.setFrame_(NSMakeRect(origin.x, origin.y, WIDTH, height))
        for v, row, x, w, h, dy in self._placed:
            v.setFrame_(NSMakeRect(x, y[row] + dy, w, h))
        for i, b in enumerate(self.variant_buttons):
            if i < n:
                # i=0 — лучший вариант, он должен оказаться сверху блока.
                b.setFrame_(NSMakeRect(PAD, y["variants"] + (n - 1 - i) * VAR_H,
                                       FULL_W, VAR_H))
                b.setHidden_(False)
                b.setAlphaValue_(1.0)
            else:
                b.setHidden_(True)

    def _paint_variants(self) -> None:
        """Заголовки и подсветка строк — перерисовываются при смене выбора."""
        for i, (text, source) in enumerate(self.variants):
            b = self.variant_buttons[i]
            selected = (i == self.variant_index)
            b.setAttributedTitle_(_variant_title(text, source, selected))
            b.layer().setBackgroundColor_(
                NSColor.selectedContentBackgroundColor().CGColor() if selected
                else NSColor.clearColor().CGColor())

    def _select_variant(self, index: int) -> None:
        if not self.variants:
            return
        self.variant_index = max(0, min(index, len(self.variants) - 1))
        self.field_right.setStringValue_(self.variants[self.variant_index][0])
        self._paint_variants()

    def variantClicked_(self, sender):
        self._select_variant(sender.tag())
        window = self.popover_view_controller.view().window()
        if window is not None:
            window.makeFirstResponder_(self.field_right)

    # --- применение правки (обе секции — по Enter, кнопок нет) -------------

    def _apply_rule(self):
        wrong = str(self.field_wrong.stringValue())
        right = str(self.field_right.stringValue())
        err = apply_fix(wrong, right)
        if err:
            self.status_label.setStringValue_(f"✗ {err}")
            return
        self.status_label.setStringValue_(f"✓ «{wrong}» → «{right}»")
        self.field_wrong.setStringValue_("")
        self.field_right.setStringValue_("")
        self._clear_variants()  # правило применено — список относился к нему
        window = self.popover_view_controller.view().window()
        if window is not None:
            window.makeFirstResponder_(self.field_wrong)

    def _learn_name(self):
        """Выучить новое имя — как `lex.py add`. Дальше решает LLM по контексту,
        поэтому здесь нет пары «как слышится / как надо»: слово само по себе
        не подменяется, оно просто становится известным."""
        name = str(self.field_name.stringValue()).strip()
        if not name:
            self.status_label.setStringValue_("✗ введи имя")
            return
        existing = TECH_TERMS_PATH.read_text(encoding="utf-8") if TECH_TERMS_PATH.exists() else ""
        if any(line.strip().lower() == name.lower() for line in existing.splitlines()):
            self.status_label.setStringValue_(f"«{name}» уже в словаре")
            return
        with TECH_TERMS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{name}\n")
        self._rebuild_lexicon(f"«{name}»")
        self.field_name.setStringValue_("")

    def _rebuild_lexicon(self, note: str = "") -> None:
        """Пересобирает lexicon.json в фоне и проверяет код выхода — раньше
        `Popen(...)` без ожидания молча съедал провал сборки (нет
        scrobbles.jsonl, битый vocab): статус говорил «пересобираю», а
        словарь оставался старым, и узнать об этом можно было только
        руками из лога."""
        prefix = f"{note} — " if note else ""
        self.status_label.setStringValue_(f"{prefix}пересобираю словарь (~10с)")

        def worker():
            # encoding явно, не text=True: тот берёт кодировку из локали
            # процесса, а внутри собранного .app она разъезжается на ascii
            # (см. Fatal Python error про init_fs_encoding в этом же .app) —
            # вывод build_lexicon.py содержит кириллицу и падал бы
            # UnicodeDecodeError'ом ровно на первом же русском слове.
            proc = subprocess.run([PYTHON3, str(PROJECT_DIR / "build_lexicon.py")],
                                  capture_output=True, encoding="utf-8")
            if proc.returncode != 0:
                log.warning("build_lexicon.py упал (%d): %s",
                           proc.returncode, proc.stderr[-2000:])
            AppHelper.callAfter(self._rebuild_done, note, proc.returncode == 0)

        threading.Thread(target=worker, daemon=True).start()

    def _rebuild_done(self, note: str, ok: bool) -> None:
        prefix = f"{note} — " if note else ""
        if ok:
            self.status_label.setStringValue_(f"✓ {prefix}словарь пересобран")
        else:
            self.status_label.setStringValue_(
                f"✗ {prefix}пересборка упала, смотри lex-widget.err.log")

    def controlTextDidChange_(self, notification):
        """Ручная правка «как надо» снимает подсветку варианта — иначе непонятно,
        что применит Enter: подсвеченный вариант или то, что реально вписано.
        `setStringValue_` (наша же подстановка при выборе/клике) этот делегатский
        метод не вызывает — он реагирует только на ввод с клавиатуры, так что
        собственный же _select_variant его не задевает."""
        if notification.object() is not self.field_right or self.variant_index == -1:
            return
        current = str(self.field_right.stringValue())
        if not self.variants or current != self.variants[self.variant_index][0]:
            self.variant_index = -1
            self._paint_variants()

    def control_textView_doCommandBySelector_(self, control, _text_view, selector):
        """Enter применяет ту секцию, в поле которой он нажат.

        Кнопок с `setKeyEquivalent_("\\r")` тут намеренно нет: клавиатурный
        эквивалент окна перехватил бы Enter раньше поля, и Enter в «выучить
        имя» применял бы правило из соседней секции.
        """
        if selector == "insertNewline:":
            if control is self.field_name:
                self._learn_name()
            else:
                self._apply_rule()
            return True
        # ↑↓ ходят по списку вариантов, не выходя из поля: руки остаются на
        # клавиатуре, а выбранный вариант тут же оказывается в «как надо»,
        # так что Enter применяет ровно то, что подсвечено.
        if selector in ("moveDown:", "moveUp:") and self.variants:
            if control is self.field_wrong or control is self.field_right:
                step = 1 if selector == "moveDown:" else -1
                self._select_variant(self.variant_index + step)
                return True
        return False

    # --- пункты меню --------------------------------------------------

    def openLearned_(self, _sender):
        subprocess.run(["open", str(PROJECT_DIR / "learned.json")])

    def rebuildLexicon_(self, _sender):
        self._rebuild_lexicon()


def main() -> None:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # без Dock-иконки
    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    app.run()


if __name__ == "__main__":
    main()
