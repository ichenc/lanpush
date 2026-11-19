# -*- coding: utf-8 -*-
"""
Windows 系统托盘支持（仅用标准库 ctypes，不依赖第三方包）

run_tray(...) 会阻塞在当前线程跑消息循环，请把网络逻辑放到别的线程。
非 Windows 平台自动降级为 no-op。
"""
import ctypes
import sys

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    from ctypes import wintypes

    # 部分 Python 版本的 wintypes 缺这些别名，运行时补齐
    for _name, _typ in (
        ("LRESULT", ctypes.c_ssize_t),
        ("WPARAM", ctypes.c_size_t),
        ("LPARAM", ctypes.c_ssize_t),
        ("HCURSOR", wintypes.HANDLE),
        ("UINT_PTR", ctypes.c_size_t),
        ("ULONG_PTR", ctypes.c_size_t),
        ("DWORD_PTR", ctypes.c_size_t),
        ("ATOM", ctypes.c_ushort),
    ):
        if not hasattr(wintypes, _name):
            setattr(wintypes, _name, _typ)

    NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
    NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 1, 2, 4, 16
    WM_USER = 0x0400
    WM_TRAYICON = WM_USER + 100
    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    WM_COMMAND = 0x0111
    WM_DESTROY = 0x0002
    MF_STRING = 0x0000
    MF_SEPARATOR = 0x0800
    TPM_RIGHTBUTTON = 0x0002
    TPM_RETURNCMD = 0x0100
    IDI_APPLICATION = 32512
    IDI_INFORMATION = 32516
    IDI_WARNING = 32515
    HWND_MESSAGE = -3
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x0010

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", ctypes.c_void_p),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hwnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeoutOrVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", wintypes.HICON),
        ]

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    WNDPROC = ctypes.WINFUNCTYPE(
        wintypes.LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    _user32.RegisterClassW.restype = wintypes.ATOM
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.DefWindowProcW.restype = wintypes.LRESULT
    _user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    _user32.SetWindowLongPtrW.restype = ctypes.c_void_p
    _user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    _user32.GetMessageW.restype = wintypes.BOOL
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    _user32.LoadIconW.restype = wintypes.HICON
    _user32.LoadImageW.argtypes = [
        wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    _user32.LoadImageW.restype = wintypes.HICON
    _user32.DestroyIcon.argtypes = [wintypes.HICON]
    _user32.CreatePopupMenu.argtypes = []
    _user32.CreatePopupMenu.restype = wintypes.HMENU
    _user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT_PTR, wintypes.LPCWSTR]
    _user32.AppendMenuW.restype = wintypes.BOOL
    _user32.TrackPopupMenu.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, ctypes.c_void_p,
    ]
    _user32.TrackPopupMenu.restype = wintypes.BOOL
    _user32.DestroyMenu.argtypes = [wintypes.HMENU]
    _user32.DestroyMenu.restype = wintypes.BOOL
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    _user32.GetCursorPos.restype = wintypes.BOOL
    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.PostMessageW.restype = wintypes.BOOL
    _shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    _shell32.Shell_NotifyIconW.restype = wintypes.BOOL


def _hiword(v):
    return (int(v) >> 16) & 0xFFFF


def _loword(v):
    return int(v) & 0xFFFF


def run_tray(title, items, on_double_click=None, icon_id=None, tip=None, icon_path=None):
    """
    阻塞运行托盘消息循环。

    title: 托盘图标悬浮提示
    items: [(命令ID, 菜单文字, 回调函数), ...]，命令ID 从 1001 开始且唯一
    on_double_click: 双击托盘图标的回调
    icon_id: 系统图标 ID（IDI_INFORMATION / IDI_APPLICATION / IDI_WARNING），作为兜底
    icon_path: 自定义 .ico 文件路径（优先使用）
    tip: 气泡通知文字（可选）
    """
    if not IS_WINDOWS:
        return False

    icon_id = icon_id if icon_id is not None else IDI_INFORMATION
    state = {"hwnd": None, "menu": {}, "nid": None, "proc": None}
    callbacks = {int(cid): cb for cid, _label, cb in items}

    def build_menu():
        hmenu = _user32.CreatePopupMenu()
        for cid, label, _cb in items:
            if label is None:
                _user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            else:
                _user32.AppendMenuW(hmenu, MF_STRING, int(cid), label)
        return hmenu

    @WNDPROC
    def wnd_proc(hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAYICON:
                ev = _loword(lparam)
                if ev == WM_RBUTTONUP:
                    pt = POINT()
                    if _user32.GetCursorPos(ctypes.byref(pt)):
                        hmenu = build_menu()
                        _user32.SetForegroundWindow(hwnd)
                        cmd = _user32.TrackPopupMenu(
                            hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                            pt.x, pt.y, 0, hwnd, None,
                        )
                        _user32.DestroyMenu(hmenu)
                        if cmd and int(cmd) in callbacks:
                            try:
                                callbacks[int(cmd)]()
                            except Exception:
                                pass
                        # 菜单关闭后发个空消息，避免下次右键菜单不弹出
                        _user32.PostMessageW(hwnd, WM_USER + 1, 0, 0)
                elif ev == WM_LBUTTONDBLCLK and on_double_click:
                    try:
                        on_double_click()
                    except Exception:
                        pass
            elif msg == WM_COMMAND:
                cid = _loword(wparam)
                if cid in callbacks:
                    try:
                        callbacks[cid]()
                    except Exception:
                        pass
            elif msg == WM_DESTROY:
                if state["nid"] is not None:
                    _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(state["nid"]))
        except Exception:
            pass
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    hinst = _kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW()
    wc.style = 0
    wc.lpfnWndProc = ctypes.cast(wnd_proc, ctypes.c_void_p).value
    wc.cbClsExtra = 0
    wc.cbWndExtra = 0
    wc.hInstance = hinst
    wc.hIcon = None
    wc.hCursor = None
    wc.lpszMenuName = None
    wc.lpszClassName = "LANPushTrayClass"
    _user32.RegisterClassW(ctypes.byref(wc))

    hwnd = _user32.CreateWindowExW(
        0, "LANPushTrayClass", "LANPushTray", 0,
        0, 0, 0, 0, HWND_MESSAGE, None, hinst, None,
    )
    if not hwnd:
        return False
    state["hwnd"] = hwnd
    state["proc"] = wnd_proc  # 防止被 GC

    # 优先加载自定义 ico，失败则回退到系统图标
    custom_icon = None
    if icon_path:
        try:
            custom_icon = _user32.LoadImageW(
                None, str(icon_path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE
            )
        except Exception:
            custom_icon = None
    if custom_icon:
        hicon = custom_icon
    else:
        hicon = _user32.LoadIconW(None, ctypes.cast(icon_id, wintypes.LPCWSTR))

    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hwnd = hwnd
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = WM_TRAYICON
    nid.hIcon = hicon
    nid.szTip = (title or "LANPush")[:127]

    if not _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
        return False
    state["nid"] = nid

    if tip:
        nid.uFlags |= NIF_INFO
        nid.szInfo = str(tip)[:255]
        nid.szInfoTitle = (title or "LANPush")[:63]
        nid.dwInfoFlags = 0
        _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    msg = wintypes.MSG()
    while True:
        ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if ret == 0 or ret == -1:
            break
        _user32.TranslateMessage(ctypes.byref(msg))
        _user32.DispatchMessageW(ctypes.byref(msg))
    return True


def tray_available():
    return IS_WINDOWS
