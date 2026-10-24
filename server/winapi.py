"""Helper Win32 per PptRemote: elenco finestre, focus, invio tasti, monitor.

Nessuna dipendenza esterna: solo ctypes e la user32/kernel32 di Windows.
Tutte le funzioni sono sicure da chiamare da thread diversi (il GIL serializza
le chiamate, e user32 non richiede apartment particolari per queste API).
"""

from __future__ import annotations

import ctypes
import os
import struct
import time
from ctypes import wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t
GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MONITOR_DEFAULTTONEAREST = 2
MONITORINFOF_PRIMARY = 1
GA_ROOT = 2
HALFTONE = 4
SRCCOPY = 0x00CC0020
BI_RGB = 0
DIB_RGB_COLORS = 0
PW_RENDERFULLCONTENT = 2
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
WM_SYSCOMMAND = 0x0112
SC_CLOSE = 0xF060
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", wt.LONG),
        ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("rcMonitor", wt.RECT),
        ("rcWork", wt.RECT),
        ("dwFlags", wt.DWORD),
        ("szDevice", wt.WCHAR * 32),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wt.BOOL, wt.HANDLE, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM
)

# ---- prototipi (importante su x64: gli handle non entrano in un int) ----
user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM), wt.LPARAM]
user32.EnumWindows.restype = wt.BOOL
user32.IsWindow.argtypes = [wt.HWND]
user32.IsWindow.restype = wt.BOOL
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.IsIconic.argtypes = [wt.HWND]
user32.IsIconic.restype = wt.BOOL
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetForegroundWindow.restype = wt.HWND
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.BringWindowToTop.argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.AttachThreadInput.restype = wt.BOOL
user32.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, ULONG_PTR]
user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ULONG_PTR
user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
user32.MonitorFromWindow.restype = wt.HANDLE
user32.MonitorFromRect.argtypes = [ctypes.POINTER(wt.RECT), wt.DWORD]
user32.MonitorFromRect.restype = wt.HANDLE
user32.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.POINTER(MONITORINFOEXW)]
user32.GetMonitorInfoW.restype = wt.BOOL
user32.EnumDisplayMonitors.argtypes = [
    wt.HDC,
    ctypes.POINTER(wt.RECT),
    MONITORENUMPROC,
    wt.LPARAM,
]
user32.EnumDisplayMonitors.restype = wt.BOOL
user32.mouse_event.argtypes = [wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ULONG_PTR]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wt.UINT
user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
user32.GetWindow.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND

user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.ReleaseDC.restype = ctypes.c_int

gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.SetStretchBltMode.argtypes = [wt.HDC, ctypes.c_int]
gdi32.StretchBlt.argtypes = [
    wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.DWORD,
]
gdi32.StretchBlt.restype = wt.BOOL
gdi32.GetDIBits.argtypes = [
    wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p,
    ctypes.POINTER(BITMAPINFO), wt.UINT,
]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteObject.restype = wt.BOOL
gdi32.DeleteDC.argtypes = [wt.HDC]
gdi32.DeleteDC.restype = wt.BOOL
user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
user32.PrintWindow.restype = wt.BOOL
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL

kernel32.GetCurrentThreadId.restype = wt.DWORD
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wt.HANDLE,
    wt.DWORD,
    wt.LPWSTR,
    ctypes.POINTER(wt.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL

# codici tasto virtuali (VK_*)
VK = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "prior": 0x21,
    "pgup": 0x21,
    "pageup": 0x21,
    "next": 0x22,
    "pgdn": 0x22,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "delete": 0x2E,
    "b": 0x42,
    "n": 0x4E,
    "p": 0x50,
    "w": 0x57,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74,
    "f6": 0x75,
    "shift": 0x10,
    "alt": 0x12,
    "ctrl": 0x11,
}
VK_SHIFT = 0x10
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
_DIGITS = {str(d): 0x30 + d for d in range(10)}


def _vk(name: str) -> int:
    name = (name or "").strip().lower()
    if len(name) == 1 and name.isdigit():
        return _DIGITS[name]
    if len(name) == 1 and name.isalpha():
        return 0x41 + (ord(name.upper()) - ord("A"))
    if name not in VK:
        raise ValueError(f"tasto non supportato: {name!r}")
    return VK[name]


def _title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _classname(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _exe_name(pid: int) -> str:
    if not pid:
        return ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(h)
    return ""


def monitors() -> list[dict]:
    """Monitor attivi, numerati 1..n ordinando per posizione (come Windows)."""
    found: list[dict] = []

    def cb(hmon, hdc, lprc, data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            found.append(
                {
                    "device": info.szDevice,
                    "primary": bool(info.dwFlags & MONITORINFOF_PRIMARY),
                    "x": info.rcMonitor.left,
                    "y": info.rcMonitor.top,
                    "width": info.rcMonitor.right - info.rcMonitor.left,
                    "height": info.rcMonitor.bottom - info.rcMonitor.top,
                }
            )
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
    found.sort(key=lambda m: (m["x"], m["y"]))
    for i, m in enumerate(found, start=1):
        m["index"] = i
        m["label"] = (
            f"Schermo {i} ({'primario' if m['primary'] else 'esteso'}) "
            f"{m['width']}x{m['height']}"
        )
    return found


def _monitor_index_for_rect(rect: wt.RECT, mons: list[dict]) -> int:
    hmon = user32.MonitorFromRect(ctypes.byref(rect), MONITOR_DEFAULTTONEAREST)
    info = MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
        return 0
    for m in mons:
        if m["device"] == info.szDevice:
            return m["index"]
    return 0


def _is_fullscreen(rect: wt.RECT, mons: list[dict]) -> bool:
    for m in mons:
        if (
            abs(rect.left - m["x"]) <= 2
            and abs(rect.top - m["y"]) <= 2
            and abs((rect.right - rect.left) - m["width"]) <= 2
            and abs((rect.bottom - rect.top) - m["height"]) <= 2
        ):
            return True
    return False


def list_windows() -> list[dict]:
    """Finestre visibili di primo livello con titolo, arricchite di processo e monitor."""
    mons = monitors()
    out: list[dict] = []
    hwnds: list[int] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, lparam):
        hwnds.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    foreground = user32.GetForegroundWindow()
    for hwnd in hwnds:
        if not user32.IsWindowVisible(hwnd):
            continue
        ex_style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        if ex_style & WS_EX_TOOLWINDOW:
            continue
        title = _title(hwnd)
        if not title:
            continue
        rect = wt.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            continue
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w < 160 or h < 120:
            continue
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.append(
            {
                "hwnd": int(hwnd),
                "title": title,
                "class": _classname(hwnd),
                "exe": _exe_name(pid.value),
                "pid": int(pid.value),
                "rect": [rect.left, rect.top, rect.right, rect.bottom],
                "monitor": _monitor_index_for_rect(rect, mons),
                "fullscreen": _is_fullscreen(rect, mons),
                "foreground": hwnd == foreground,
                "minimized": bool(user32.IsIconic(hwnd)),
            }
        )
    return out


def get_window(hwnd: int) -> dict | None:
    for w in list_windows():
        if w["hwnd"] == int(hwnd):
            return w
    return None


def is_window(hwnd: int) -> bool:
    return bool(hwnd) and bool(user32.IsWindow(int(hwnd)))


def root_window(hwnd: int) -> int:
    """Finestra di primo livello che contiene hwnd (per i widget Tk)."""
    if not hwnd:
        return 0
    root = user32.GetAncestor(int(hwnd), GA_ROOT)
    return int(root or hwnd)


def focus(hwnd: int) -> bool:
    """Porta la finestra in primo piano (con i soliti trucchi per il foreground lock)."""
    hwnd = int(hwnd or 0)
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    if user32.GetForegroundWindow() == hwnd:
        return True

    cur = kernel32.GetCurrentThreadId()
    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    tgt_tid = user32.GetWindowThreadProcessId(hwnd, None)
    attached: list[int] = []
    for tid in {fg_tid, tgt_tid}:
        if tid and tid != cur:
            if user32.AttachThreadInput(cur, tid, True):
                attached.append(tid)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        for tid in attached:
            user32.AttachThreadInput(cur, tid, False)

    if user32.GetForegroundWindow() != hwnd:
        # trucco del tasto Alt: sblocca il vincolo di foreground
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        user32.SetForegroundWindow(hwnd)
    return user32.GetForegroundWindow() == hwnd


def send_keys(names: list[str], delay: float = 0.035) -> None:
    """Invia una sequenza di tasti (per nome VK) alla finestra attiva."""
    events: list[INPUT] = []
    for name in names:
        vk = _vk(name)
        events.append(INPUT(type=1, u=_INPUTUNION(ki=KEYBDINPUT(vk, 0, 0, 0, 0))))
        events.append(
            INPUT(type=1, u=_INPUTUNION(ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, 0)))
        )
        if delay:
            # invia subito questa coppia per rispettare l'ordine con le pause
            arr = (INPUT * 2)(events[0], events[1])
            user32.SendInput(2, arr, ctypes.sizeof(INPUT))
            events.clear()
            time.sleep(delay)
    if events:
        arr = (INPUT * len(events))(*events)
        user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))


def post_keys(hwnd: int, names: list[str], delay: float = 0.06) -> bool:
    """Manda i tasti direttamente alla finestra, senza portarla in primo piano.

    Serve per le finestre che Windows non lascia attivare (es. Visualizzazione
    protetta di PowerPoint): molti programmi gestiscono comunque i messaggi.
    """
    hwnd = int(hwnd or 0)
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    for name in names:
        vk = _vk(name)
        user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 0)
        time.sleep(0.02)
        user32.PostMessageW(hwnd, WM_KEYUP, vk, 0)
        time.sleep(delay)
    return True


def press(names: list[str], hwnd: int = 0, delay: float = 0.04) -> bool:
    """Porta la finestra in primo piano e preme i tasti indicati.

    Se Windows non concede il primo piano, manda i tasti direttamente alla finestra.
    """
    if hwnd:
        if not focus(hwnd):
            return post_keys(hwnd, names)
        time.sleep(0.06)
    send_keys(names, delay)
    return True


def capture_window_bmp(hwnd: int, max_width: int = 300) -> bytes | None:
    """Cattura la finestra e restituisce un BMP ridotto (anteprima live).

    Prova prima PrintWindow (così cattura anche le presentazioni a schermo
    intero con accelerazione hardware), poi ripiega sui pixel dello schermo.
    """
    hwnd = int(hwnd or 0)
    if not hwnd or not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
        return None
    rect = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width < 16 or height < 16:
        return None

    scale = min(1.0, float(max_width) / float(width))
    tw = max(16, int(width * scale))
    th = max(9, int(height * scale))
    row = (tw * 3 + 3) & ~3
    size = row * th

    hdc_screen = user32.GetDC(None)
    if not hdc_screen:
        return None
    full_dc = None
    full_bmp = None
    small_dc = None
    small_bmp = None
    try:
        # 1) cattura della finestra (funziona anche con overlay/DirectComposition)
        full_dc = gdi32.CreateCompatibleDC(hdc_screen)
        full_bmp = gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
        if not full_dc or not full_bmp:
            return None
        gdi32.SelectObject(full_dc, full_bmp)
        captured = bool(user32.PrintWindow(hwnd, full_dc, PW_RENDERFULLCONTENT))

        # 2) riduzione nella misura richiesta
        small_dc = gdi32.CreateCompatibleDC(hdc_screen)
        small_bmp = gdi32.CreateCompatibleBitmap(hdc_screen, tw, th)
        if not small_dc or not small_bmp:
            return None
        gdi32.SelectObject(small_dc, small_bmp)
        gdi32.SetStretchBltMode(small_dc, HALFTONE)

        if captured:
            gdi32.StretchBlt(
                small_dc, 0, 0, tw, th, full_dc, 0, 0, width, height, SRCCOPY
            )
        else:
            gdi32.StretchBlt(
                small_dc, 0, 0, tw, th, hdc_screen, rect.left, rect.top,
                width, height, SRCCOPY,
            )

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = tw
        bmi.bmiHeader.biHeight = th
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 24
        bmi.bmiHeader.biCompression = BI_RGB
        bmi.bmiHeader.biSizeImage = size
        buffer = ctypes.create_string_buffer(size)
        if not gdi32.GetDIBits(
            small_dc, small_bmp, 0, th, buffer, ctypes.byref(bmi), DIB_RGB_COLORS
        ):
            return None

        # 3) se è tutto nero, riprova leggendo lo schermo
        raw = buffer.raw
        if captured and max(raw) < 12:
            gdi32.StretchBlt(
                small_dc, 0, 0, tw, th, hdc_screen, rect.left, rect.top,
                width, height, SRCCOPY,
            )
            buffer = ctypes.create_string_buffer(size)
            if not gdi32.GetDIBits(
                small_dc, small_bmp, 0, th, buffer, ctypes.byref(bmi), DIB_RGB_COLORS
            ):
                return None
            raw = buffer.raw

        header = struct.pack("<2sIHHI", b"BM", 14 + 40 + size, 0, 0, 54)
        header += struct.pack(
            "<IiiHHIIiiII", 40, tw, th, 1, 24, BI_RGB, size, 2835, 2835, 0, 0
        )
        return header + raw
    finally:
        if small_bmp:
            gdi32.DeleteObject(small_bmp)
        if small_dc:
            gdi32.DeleteDC(small_dc)
        if full_bmp:
            gdi32.DeleteObject(full_bmp)
        if full_dc:
            gdi32.DeleteDC(full_dc)
        user32.ReleaseDC(None, hdc_screen)


def _key_event(vk: int, down: bool) -> None:
    user32.keybd_event(vk, 0, 0 if down else KEYEVENTF_KEYUP, 0)


def press_combo(modifier: str, key: str, hwnd: int = 0, delay: float = 0.05) -> bool:
    """Porta la finestra in primo piano e preme Modificatore+Tasto (es. Ctrl+L)."""
    if hwnd and not focus(hwnd):
        return False
    time.sleep(0.06)
    mod = _vk(modifier)
    tap = _vk(key)
    _key_event(mod, True)
    time.sleep(delay)
    _key_event(tap, True)
    time.sleep(0.02)
    _key_event(tap, False)
    time.sleep(delay)
    _key_event(mod, False)
    return True


def monitor_rect(index: int = 0) -> tuple[int, int, int, int] | None:
    """Rettangolo (left, top, right, bottom) del monitor 1..n, o 0 = tutti."""
    if index and index > 0:
        for mon in monitors():
            if mon["index"] == index:
                return (mon["x"], mon["y"], mon["x"] + mon["width"], mon["y"] + mon["height"])
        return None
    left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if width <= 0 or height <= 0:
        return None
    return (left, top, left + width, top + height)


def capture_screen_bmp(monitor_index: int = 0, max_width: int = 640) -> bytes | None:
    """Cattura lo schermo (o un monitor) e restituisce un BMP ridotto."""
    rect = monitor_rect(monitor_index)
    if not rect:
        return None
    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    if width < 16 or height < 16:
        return None
    scale = min(1.0, float(max_width) / float(width))
    tw = max(16, int(width * scale))
    th = max(9, int(height * scale))
    row = (tw * 3 + 3) & ~3
    size = row * th
    hdc_screen = user32.GetDC(None)
    if not hdc_screen:
        return None
    hdc_mem = None
    hbmp = None
    try:
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, tw, th)
        if not hdc_mem or not hbmp:
            return None
        gdi32.SelectObject(hdc_mem, hbmp)
        gdi32.SetStretchBltMode(hdc_mem, HALFTONE)
        if not gdi32.StretchBlt(hdc_mem, 0, 0, tw, th, hdc_screen, left, top, width, height, SRCCOPY):
            return None
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = tw
        bmi.bmiHeader.biHeight = th
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 24
        bmi.bmiHeader.biCompression = BI_RGB
        bmi.bmiHeader.biSizeImage = size
        buffer = ctypes.create_string_buffer(size)
        if not gdi32.GetDIBits(hdc_mem, hbmp, 0, th, buffer, ctypes.byref(bmi), DIB_RGB_COLORS):
            return None
        header = struct.pack("<2sIHHI", b"BM", 14 + 40 + size, 0, 0, 54)
        header += struct.pack("<IiiHHIIiiII", 40, tw, th, 1, 24, BI_RGB, size, 2835, 2835, 0, 0)
        return header + buffer.raw
    finally:
        if hbmp:
            gdi32.DeleteObject(hbmp)
        if hdc_mem:
            gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)


def close_window(hwnd: int) -> bool:
    """Chiede alla finestra di chiudersi (come il pulsante X), senza rubare il focus."""
    hwnd = int(hwnd or 0)
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    user32.PostMessageW(hwnd, WM_SYSCOMMAND, SC_CLOSE, 0)
    return True


def move_mouse(dx: int, dy: int) -> None:
    """Sposta il puntatore in modo relativo (genera un vero movimento del mouse)."""
    user32.mouse_event(MOUSEEVENTF_MOVE, int(dx) & 0xFFFFFFFF, int(dy) & 0xFFFFFFFF, 0, 0)


def move_mouse_abs(x: int, y: int) -> None:
    """Porta il puntatore a una posizione assoluta e forza un evento di movimento."""
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.005)
    user32.mouse_event(MOUSEEVENTF_MOVE, 1, 0, 0, 0)
    user32.mouse_event(MOUSEEVENTF_MOVE, -1, 0, 0, 0)


def click_mouse() -> None:
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.03)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def foreground_hwnd() -> int:
    return int(user32.GetForegroundWindow() or 0)
