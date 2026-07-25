"""Глобальный хоткей через Carbon RegisterEventHotKey.

Почему Carbon, а не `NSEvent.addGlobalMonitorForEventsMatchingMask_`: монитор
NSEvent требует разрешения Accessibility и, главное, **не потребляет** событие —
хоткей сработал бы и у нас, и в активном приложении одновременно. Carbon
регистрирует настоящий системный хоткей: разрешений не просит, событие забирает
себе.

Регистрация идёт по **virtual key code** — коду физической клавиши, а не по
символу. Поэтому одна регистрация покрывает обе раскладки: Cmd+Shift+S на
латинице и Cmd+Shift+Ы на кириллице — это одна и та же клавиша, код 1. Этот
же код совпадает с `NSEvent.keyCode()` — Carbon и Cocoa используют одно и то же
пространство кодов, поэтому комбинацию, снятую с NSEvent (см. `settings.py`
в menubar.py), можно передавать сюда без пересчёта.
"""

import ctypes
import ctypes.util
import json
from pathlib import Path

_carbon = ctypes.CDLL(ctypes.util.find_library("Carbon"))

kEventClassKeyboard = 0x6B657962  # 'keyb'
kEventHotKeyPressed = 5
noErr = 0

cmdKey = 0x0100
shiftKey = 0x0200
optionKey = 0x0800
controlKey = 0x1000

# NSEvent.modifierFlags() использует другие битовые значения — таблица
# перевода в карбоновские константы выше.
NS_CMD = 1 << 20
NS_SHIFT = 1 << 17
NS_OPTION = 1 << 19
NS_CONTROL = 1 << 18

VK_S = 1  # kVK_ANSI_S — физическая клавиша S, она же Ы в русской раскладке

SETTINGS_PATH = Path(__file__).resolve().parent / "settings.json"
DEFAULT_KEYCODE = VK_S
DEFAULT_MODIFIERS = cmdKey | shiftKey

# Только те клавиши, у которых есть разумное отображаемое имя — recorder
# принимает и остальные (хранит код), но подписывает как «код N».
_KEYCODE_NAMES = {
    0: "A", 1: "S", 2: "D", 3: "F", 4: "H", 5: "G", 6: "Z", 7: "X", 8: "C",
    9: "V", 11: "B", 12: "Q", 13: "W", 14: "E", 15: "R", 16: "Y", 17: "T",
    31: "O", 32: "U", 34: "I", 35: "P", 37: "L", 38: "J", 40: "K", 45: "N",
    46: "M", 18: "1", 19: "2", 20: "3", 21: "4", 22: "6", 23: "5", 25: "9",
    26: "7", 28: "8", 29: "0", 36: "↩", 48: "⇥", 49: "Space", 51: "⌫", 53: "⎋",
}


def carbon_mods_from_ns(flags: int) -> int:
    """NSEvent.modifierFlags() -> битовая маска Carbon для RegisterEventHotKey."""
    mods = 0
    if flags & NS_CMD:
        mods |= cmdKey
    if flags & NS_SHIFT:
        mods |= shiftKey
    if flags & NS_OPTION:
        mods |= optionKey
    if flags & NS_CONTROL:
        mods |= controlKey
    return mods


def describe(keycode: int, modifiers: int) -> str:
    """(1, cmdKey|shiftKey) -> «⌘⇧S» — для показа в UI."""
    parts = []
    if modifiers & controlKey:
        parts.append("⌃")
    if modifiers & optionKey:
        parts.append("⌥")
    if modifiers & shiftKey:
        parts.append("⇧")
    if modifiers & cmdKey:
        parts.append("⌘")
    parts.append(_KEYCODE_NAMES.get(keycode, f"код {keycode}"))
    return "".join(parts)


def load() -> tuple:
    """Сохранённая комбинация или дефолт (Cmd+Shift+S), если файла ещё нет."""
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return int(data["keycode"]), int(data["modifiers"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return DEFAULT_KEYCODE, DEFAULT_MODIFIERS


def save(keycode: int, modifiers: int) -> None:
    SETTINGS_PATH.write_text(json.dumps({"keycode": keycode, "modifiers": modifiers}),
                              encoding="utf-8")


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


_HandlerProc = ctypes.CFUNCTYPE(
    ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)

_carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
_carbon.InstallEventHandler.argtypes = [
    ctypes.c_void_p, _HandlerProc, ctypes.c_uint32,
    ctypes.POINTER(_EventTypeSpec), ctypes.c_void_p, ctypes.c_void_p]
_carbon.InstallEventHandler.restype = ctypes.c_int32
_carbon.RegisterEventHotKey.argtypes = [
    ctypes.c_uint32, ctypes.c_uint32, _EventHotKeyID,
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
_carbon.RegisterEventHotKey.restype = ctypes.c_int32
_carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
_carbon.UnregisterEventHotKey.restype = ctypes.c_int32

# Carbon держит только сырые указатели. Без своих ссылок Python соберёт
# callback мусором, и хоткей молча перестанет срабатывать.
_keep_alive: list = []
_hotkey_ref = None  # текущая зарегистрированная комбинация — для unregister
_callback = None     # текущий обработчик — переиспользуется при смене комбинации
_target = None


def _ensure_handler() -> bool:
    """Обработчик события ставится один раз на весь процесс — дальше меняется
    только сама зарегистрированная комбинация (unregister + register)."""
    global _target
    if _target is not None:
        return True

    def _on_hotkey(_next_handler, _event, _user_data):
        try:
            if _callback is not None:
                _callback()
        except Exception:  # noqa: BLE001 — из C-callback исключение не выпустить
            import logging
            logging.getLogger("hotkey").exception("обработчик хоткея упал")
        return noErr

    proc = _HandlerProc(_on_hotkey)
    spec = _EventTypeSpec(kEventClassKeyboard, kEventHotKeyPressed)
    target = _carbon.GetApplicationEventTarget()

    if _carbon.InstallEventHandler(
            target, proc, 1, ctypes.byref(spec), None, None) != noErr:
        return False

    _keep_alive.append(proc)
    _target = target
    return True


def register(callback, keycode: int = DEFAULT_KEYCODE,
             modifiers: int = DEFAULT_MODIFIERS) -> bool:
    """Ставит глобальный хоткей. Возвращает False, если система отказала
    (обычно — комбинация уже занята другим приложением)."""
    global _callback, _hotkey_ref
    if not _ensure_handler():
        return False

    if _hotkey_ref is not None:
        _carbon.UnregisterEventHotKey(_hotkey_ref)
        _keep_alive.remove(_hotkey_ref)
        _hotkey_ref = None

    _callback = callback
    hotkey_ref = ctypes.c_void_p()
    status = _carbon.RegisterEventHotKey(
        keycode, modifiers, _EventHotKeyID(0x6C657800, 1),  # 'lex\0'
        _target, 0, ctypes.byref(hotkey_ref))
    if status != noErr:
        return False

    _keep_alive.append(hotkey_ref)
    _hotkey_ref = hotkey_ref
    return True
