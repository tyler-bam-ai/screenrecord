"""Reliable Windows keyboard capture using the Raw Input API.

``pynput`` uses a ``WH_KEYBOARD_LL`` hook on Windows.  Microsoft documents
that Windows can silently remove that hook when its callback misses the hook
timeout; the listener thread can remain alive even though normal keys are no
longer delivered.  Raw Input is asynchronous and is Microsoft's recommended
monitoring path for this case.

This module owns a hidden message-only window on a dedicated thread, registers
for keyboard HID input with ``RIDEV_INPUTSINK``, translates key-down records
using the active foreground keyboard layout, and forwards the resulting value
to ``InputMonitor``.  It has no third-party dependencies and is imported only
on Windows.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from ctypes import wintypes
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ``ctypes.wintypes`` is intentionally sparse on non-Windows hosts.  Keeping
# this module importable there lets the pure translation tests run in CI/macOS;
# the listener itself still refuses to start unless ``os.name == 'nt'``.
HCURSOR = getattr(wintypes, "HCURSOR", wintypes.HANDLE)


WM_INPUT = 0x00FF
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
RID_INPUT = 0x10000003
RIM_TYPEKEYBOARD = 1
RIDEV_INPUTSINK = 0x00000100
RI_KEY_BREAK = 0x0001
RI_KEY_E0 = 0x0002

VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_CAPITAL = 0x14
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_PRIOR = 0x21
VK_NEXT = 0x22
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_DELETE = 0x2E
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_NUMLOCK = 0x90
VK_SCROLL = 0x91
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5


SPECIAL_KEYS = {
    VK_BACK: "Key.backspace",
    VK_TAB: "Key.tab",
    VK_RETURN: "Key.enter",
    VK_ESCAPE: "Key.esc",
    VK_SPACE: "Key.space",
    VK_PRIOR: "Key.page_up",
    VK_NEXT: "Key.page_down",
    VK_END: "Key.end",
    VK_HOME: "Key.home",
    VK_LEFT: "Key.left",
    VK_UP: "Key.up",
    VK_RIGHT: "Key.right",
    VK_DOWN: "Key.down",
    VK_DELETE: "Key.delete",
    VK_LWIN: "Key.cmd",
    VK_RWIN: "Key.cmd",
    VK_NUMLOCK: "Key.num_lock",
    VK_SCROLL: "Key.scroll_lock",
    VK_LSHIFT: "Key.shift",
    VK_RSHIFT: "Key.shift",
    VK_LCONTROL: "Key.ctrl_l",
    VK_RCONTROL: "Key.ctrl_r",
    VK_LMENU: "Key.alt_l",
    VK_RMENU: "Key.alt_r",
}


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wintypes.USHORT),
        ("usUsage", wintypes.USHORT),
        ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    ]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wintypes.DWORD),
        ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam", wintypes.WPARAM),
    ]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [
        ("MakeCode", wintypes.USHORT),
        ("Flags", wintypes.USHORT),
        ("Reserved", wintypes.USHORT),
        ("VKey", wintypes.USHORT),
        ("Message", wintypes.UINT),
        ("ExtraInformation", wintypes.ULONG),
    ]


class RAWINPUTUNION(ctypes.Union):
    _fields_ = [("keyboard", RAWKEYBOARD)]


class RAWINPUT(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("data", RAWINPUTUNION)]


WINFUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
LRESULT = ctypes.c_ssize_t
WNDPROC = WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", HCURSOR),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def normalize_raw_vkey(vkey: int, make_code: int, flags: int) -> int:
    """Resolve generic modifier virtual keys to their left/right variants."""
    if vkey == VK_SHIFT:
        return VK_RSHIFT if make_code == 0x36 else VK_LSHIFT
    if vkey == VK_CONTROL:
        return VK_RCONTROL if flags & RI_KEY_E0 else VK_LCONTROL
    if vkey == VK_MENU:
        return VK_RMENU if flags & RI_KEY_E0 else VK_LMENU
    return vkey


class WindowsRawKeyboardListener:
    """Background Raw Input receiver compatible with ``pynput.Listener``."""

    def __init__(self, on_press: Callable[[str], None]) -> None:
        self._on_press = on_press
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._startup_error: Optional[BaseException] = None
        self._hwnd = None
        self._class_name = ""
        self._wndproc = None
        self._down: set[int] = set()
        self._toggles: set[int] = set()
        self._user32 = None
        self._kernel32 = None

    def start(self) -> None:
        if os.name != "nt":
            raise OSError("Windows Raw Input is available only on Windows")
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(
            target=self._run,
            name="windows-raw-keyboard",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("Windows Raw Input listener did not become ready")
        if self._startup_error is not None:
            raise RuntimeError("Windows Raw Input listener failed") from self._startup_error

    def stop(self) -> None:
        user32 = self._user32
        hwnd = self._hwnd
        if user32 is not None and hwnd:
            try:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except Exception:
                logger.debug("Could not post Raw Input close message", exc_info=True)
        if self._thread is not None:
            self._thread.join(timeout=3)

    def is_alive(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    def _configure_apis(self) -> None:
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u = self._user32
        k = self._kernel32

        u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        u.RegisterClassW.restype = wintypes.ATOM
        u.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]
        u.CreateWindowExW.restype = wintypes.HWND
        u.DefWindowProcW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        ]
        u.DefWindowProcW.restype = LRESULT
        u.DestroyWindow.argtypes = [wintypes.HWND]
        u.DestroyWindow.restype = wintypes.BOOL
        u.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        u.UnregisterClassW.restype = wintypes.BOOL
        u.RegisterRawInputDevices.argtypes = [
            ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT
        ]
        u.RegisterRawInputDevices.restype = wintypes.BOOL
        u.GetRawInputData.argtypes = [
            wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID,
            ctypes.POINTER(wintypes.UINT), wintypes.UINT,
        ]
        u.GetRawInputData.restype = wintypes.UINT
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
        ]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.GetKeyboardLayout.argtypes = [wintypes.DWORD]
        u.GetKeyboardLayout.restype = wintypes.HANDLE
        u.GetKeyState.argtypes = [ctypes.c_int]
        u.GetKeyState.restype = wintypes.SHORT
        u.ToUnicodeEx.argtypes = [
            wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_ubyte),
            wintypes.LPWSTR, ctypes.c_int, wintypes.UINT, wintypes.HANDLE,
        ]
        u.ToUnicodeEx.restype = ctypes.c_int
        u.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT
        ]
        u.GetMessageW.restype = wintypes.BOOL
        u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        u.PostMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        ]
        u.PostQuitMessage.argtypes = [ctypes.c_int]
        k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        k.GetModuleHandleW.restype = wintypes.HMODULE

    def _run(self) -> None:
        hinstance = None
        try:
            self._configure_apis()
            assert self._user32 is not None and self._kernel32 is not None
            user32 = self._user32
            hinstance = self._kernel32.GetModuleHandleW(None)
            self._class_name = f"ScreenRecorderRawKeyboard_{os.getpid()}_{id(self):x}"

            @WNDPROC
            def wndproc(hwnd, message, wparam, lparam):
                try:
                    if message == WM_INPUT:
                        self._handle_raw_input(lparam)
                    elif message == WM_CLOSE:
                        user32.DestroyWindow(hwnd)
                        return 0
                    elif message == WM_DESTROY:
                        user32.PostQuitMessage(0)
                        return 0
                except Exception:
                    logger.exception("Windows Raw Input callback failed")
                return user32.DefWindowProcW(hwnd, message, wparam, lparam)

            self._wndproc = wndproc
            wc = WNDCLASSW()
            wc.lpfnWndProc = wndproc
            wc.hInstance = hinstance
            wc.lpszClassName = self._class_name
            if not user32.RegisterClassW(ctypes.byref(wc)):
                raise ctypes.WinError(ctypes.get_last_error())

            self._hwnd = user32.CreateWindowExW(
                0, self._class_name, self._class_name, 0,
                0, 0, 0, 0, None, None, hinstance, None,
            )
            if not self._hwnd:
                raise ctypes.WinError(ctypes.get_last_error())

            device = RAWINPUTDEVICE(0x01, 0x06, RIDEV_INPUTSINK, self._hwnd)
            if not user32.RegisterRawInputDevices(
                ctypes.byref(device), 1, ctypes.sizeof(RAWINPUTDEVICE)
            ):
                raise ctypes.WinError(ctypes.get_last_error())

            for vk in (VK_CAPITAL, VK_NUMLOCK, VK_SCROLL):
                if user32.GetKeyState(vk) & 1:
                    self._toggles.add(vk)
            self._ready.set()
            logger.info("Windows Raw Input keyboard listener ready.")

            msg = wintypes.MSG()
            while True:
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except BaseException as exc:
            self._startup_error = exc
            logger.exception("Windows Raw Input listener stopped unexpectedly")
            self._ready.set()
        finally:
            if self._user32 is not None and self._class_name and hinstance:
                try:
                    self._user32.UnregisterClassW(self._class_name, hinstance)
                except Exception:
                    pass
            self._hwnd = None

    def _handle_raw_input(self, handle) -> None:
        assert self._user32 is not None
        size = wintypes.UINT(0)
        header_size = ctypes.sizeof(RAWINPUTHEADER)
        result = self._user32.GetRawInputData(
            handle, RID_INPUT, None, ctypes.byref(size), header_size
        )
        if result == 0xFFFFFFFF or size.value < ctypes.sizeof(RAWINPUTHEADER):
            return
        buffer = ctypes.create_string_buffer(size.value)
        result = self._user32.GetRawInputData(
            handle, RID_INPUT, buffer, ctypes.byref(size), header_size
        )
        if result == 0xFFFFFFFF:
            return
        raw = ctypes.cast(buffer, ctypes.POINTER(RAWINPUT)).contents
        if raw.header.dwType != RIM_TYPEKEYBOARD:
            return
        key = raw.data.keyboard
        vkey = normalize_raw_vkey(int(key.VKey), int(key.MakeCode), int(key.Flags))
        if vkey in (0, 0xFF):
            return

        is_break = bool(key.Flags & RI_KEY_BREAK)
        if is_break:
            self._down.discard(vkey)
            return

        self._down.add(vkey)
        if vkey in (VK_CAPITAL, VK_NUMLOCK, VK_SCROLL):
            if vkey in self._toggles:
                self._toggles.remove(vkey)
            else:
                self._toggles.add(vkey)
        value = self._key_value(vkey, int(key.MakeCode))
        if value:
            self._on_press(value)

    def _key_value(self, vkey: int, make_code: int) -> str:
        special = SPECIAL_KEYS.get(vkey)
        if special is not None:
            return special
        assert self._user32 is not None

        state = (ctypes.c_ubyte * 256)()
        for down in self._down:
            if 0 <= down < 256:
                state[down] = 0x80
        # Generic modifier slots are also required by ToUnicodeEx.
        if VK_LSHIFT in self._down or VK_RSHIFT in self._down:
            state[VK_SHIFT] = 0x80
        if VK_LCONTROL in self._down or VK_RCONTROL in self._down:
            state[VK_CONTROL] = 0x80
        if VK_LMENU in self._down or VK_RMENU in self._down:
            state[VK_MENU] = 0x80
        for toggle in self._toggles:
            state[toggle] |= 0x01

        foreground = self._user32.GetForegroundWindow()
        thread_id = self._user32.GetWindowThreadProcessId(foreground, None)
        layout = self._user32.GetKeyboardLayout(thread_id)
        chars = ctypes.create_unicode_buffer(8)
        count = self._user32.ToUnicodeEx(
            vkey, make_code, state, chars, len(chars), 0, layout
        )
        if count > 0:
            return chars.value[:count]
        if count < 0:
            # Clear the dead-key state so it cannot poison later translations.
            self._user32.ToUnicodeEx(
                vkey, make_code, state, chars, len(chars), 0, layout
            )
            return chars.value[:1]
        return f"Key.vk_{vkey:02x}"
