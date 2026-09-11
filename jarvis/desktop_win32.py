"""The Windows seam for Desktop basic: one validated operation, one backend.

``run_desktop_operation`` is the ORDER of checks, and the order is the safety
property: every refusal happens before anything is dispatched, and once
input may have reached an application nothing short of Windows' own count is
treated as an answer. The backend (``Win32Desktop``) is primitives only -
enumerate, inspect, focus, inject - over the standard library's ``ctypes``:
user32/kernel32 for windows, processes and ``SendInput``, and the operating
system's own UI Automation client (``IUIAutomation``) for the accessibility
tree. No dependency, no helper process, no screenshot, and no call that reads
a field's value.

OUTCOMES. ``DesktopExecutionUnavailable`` is a DEFINITE outcome: nothing was
dispatched, or - for a focus Windows declined - the observed end state is
known. ``DesktopOutcomeUnknown`` means input may have been dispatched and its
effect is not established; it is never replayed.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from typing import Any, Callable

from jarvis.desktop import (
    DESKTOP_KEYS,
    DESKTOP_TEXT_ROLES,
    DesktopElement,
    DesktopExecutionUnavailable,
    DesktopOutcomeUnknown,
    DesktopRequestFrame,
    DesktopWindow,
    desktop_element_role,
    desktop_ref_index,
    desktop_rendered_elements,
    desktop_view_digest,
    desktop_window_in_app,
    desktop_window_rendering,
    desktop_window_view,
    parse_desktop_window_id,
)

DESKTOP_TIMEOUT_SECONDS = 60
#: How long a launch waits for the new program to be ready for input. A
#: program that is not ready in time is still reported as launched.
DESKTOP_LAUNCH_READY_MS = 10_000
#: How many UI Automation elements one inspection walks before stopping.
DESKTOP_MAX_SCANNED = 500


# --------------------------------------------------------------------------
# The operation - the order of checks
# --------------------------------------------------------------------------


class _Dispatch:
    started = False


def run_desktop_operation(
    frame: DesktopRequestFrame,
    *,
    cancelled: Callable[[], bool] = lambda: False,
    backend_factory: Callable[[], Any] = None,
) -> dict[str, Any]:
    """Run one validated frame against a fresh backend.

    Anything raised before dispatch is a refusal; anything unclassified raised
    after dispatch is an unknown outcome.
    """
    dispatch = _Dispatch()
    try:
        with (backend_factory or Win32Desktop)() as desk:
            return _OPERATIONS[frame.capability](desk, frame, cancelled, dispatch)
    except (DesktopExecutionUnavailable, DesktopOutcomeUnknown):
        raise
    except Exception as exc:  # noqa: BLE001 - classified by whether input left
        if dispatch.started:
            raise DesktopOutcomeUnknown(
                f"the {frame.capability} was dispatched and did not complete"
            ) from exc
        raise DesktopExecutionUnavailable(
            f"the {frame.capability} could not be prepared: {type(exc).__name__}"
        ) from exc


def _window(desk: Any, frame: DesktopRequestFrame) -> DesktopWindow:
    """The live window the approval named, or fail closed.

    Matched on the handle, then required to still be owned by the SAME
    process - same id, same creation time - running the authorized image. A
    handle Windows reused, or a process id reused by another process, is a
    different window and the approval was never for it.
    """
    hwnd, pid, created = parse_desktop_window_id(frame.resource.window_id)
    matches = [window for window in desk.windows() if window.hwnd == hwnd]
    if len(matches) != 1:
        raise DesktopExecutionUnavailable("the window no longer exists")
    window = matches[0]
    if (window.pid, window.created) != (pid, created):
        raise DesktopExecutionUnavailable(
            "the window now belongs to a different process, so the approval no longer describes it"
        )
    if not desktop_window_in_app(window, frame.app):
        raise DesktopExecutionUnavailable("the window is outside the authorized application")
    return window


def _view(desk: Any, window: DesktopWindow) -> tuple[str, list[DesktopElement]]:
    elements = desktop_rendered_elements(desk.elements(window.hwnd))
    return desktop_window_view(window, elements), elements


def _target(desk: Any, frame: DesktopRequestFrame) -> tuple[DesktopWindow, DesktopElement]:
    """The approved element in the approved window, still exactly as inspected."""
    window = _window(desk, frame)
    if window.minimized:
        raise DesktopExecutionUnavailable("the window is minimized; focus it first")
    rendering, elements = _view(desk, window)
    if desktop_view_digest(rendering) != frame.resource.digest:
        raise DesktopExecutionUnavailable(
            "the window changed since it was inspected, so the approved target no longer describes it"
        )
    element = elements[desktop_ref_index(rendering, frame.resource.ref) - 1]
    if not element.enabled:
        raise DesktopExecutionUnavailable("the target element is disabled")
    if desk.foreground() != window.hwnd:
        raise DesktopExecutionUnavailable("the window is not in the foreground; focus it first")
    if desk.input_blocked(window.pid):
        # UIPI drops input to an elevated window SILENTLY - SendInput still
        # reports success - so an injection there could never be confirmed.
        raise DesktopExecutionUnavailable(
            "the application runs elevated; Windows would silently drop the input"
        )
    return window, element


def _not_cancelled(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise DesktopExecutionUnavailable("the operation was cancelled before anything was dispatched")


def _injected(sent: int, expected: int) -> None:
    """Windows' own count is the only answer. None inserted is definite; a
    partial insertion means some input left and the rest did not."""
    if sent == expected:
        return
    if sent == 0:
        raise DesktopExecutionUnavailable("Windows accepted none of the input")
    raise DesktopOutcomeUnknown(f"Windows accepted {sent} of {expected} input events")


def _list(desk: Any, frame: DesktopRequestFrame, cancelled: Any, dispatch: _Dispatch) -> dict:
    return {"content": desktop_window_rendering(desk.windows(), app=frame.app)}


def _inspect(desk: Any, frame: DesktopRequestFrame, cancelled: Any, dispatch: _Dispatch) -> dict:
    return {"content": _view(desk, _window(desk, frame))[0]}


def _launch(desk: Any, frame: DesktopRequestFrame, cancelled: Any, dispatch: _Dispatch) -> dict:
    _not_cancelled(cancelled)
    dispatch.started = True
    pid, ready = desk.launch(frame.app)
    return {"pid": pid, "ready": ready}


def _focus(desk: Any, frame: DesktopRequestFrame, cancelled: Any, dispatch: _Dispatch) -> dict:
    window = _window(desk, frame)
    _not_cancelled(cancelled)
    dispatch.started = True
    desk.bring_to_front(window.hwnd)
    if desk.foreground() != window.hwnd:
        # A definite, observed end state rather than a guess: Windows kept
        # another window in front (its foreground lock), so nothing typed or
        # clicked next could land here.
        raise DesktopExecutionUnavailable(
            "Windows kept another window in the foreground; the window may have "
            "been restored but it is not focused"
        )
    return {"window_id": window.window_id}


def _click(desk: Any, frame: DesktopRequestFrame, cancelled: Any, dispatch: _Dispatch) -> dict:
    window, element = _target(desk, frame)
    left, top, right, bottom = element.rect
    x, y = (left + right) // 2, (top + bottom) // 2
    if desk.root_at(x, y) != window.hwnd:
        raise DesktopExecutionUnavailable("another window covers the target element")
    _not_cancelled(cancelled)
    # The pointer is placed first and CHECKED: a move clicks nothing, and a
    # pointer that did not land on the approved window refuses the click.
    if not desk.move_cursor(x, y):
        raise DesktopExecutionUnavailable("the pointer could not be placed on the target")
    cx, cy = desk.cursor()
    if abs(cx - x) > 1 or abs(cy - y) > 1 or desk.root_at(cx, cy) != window.hwnd:
        raise DesktopExecutionUnavailable("the pointer did not land on the target element")
    if desk.foreground() != window.hwnd:
        raise DesktopExecutionUnavailable("the window lost the foreground before the click")
    # ponytail: another process can still take the foreground between this
    # check and SendInput; SendInput is atomic only against other input.
    dispatch.started = True
    _injected(desk.click(x, y), 3)
    return {"window_id": window.window_id}


def _focused_field(desk: Any, frame: DesktopRequestFrame, cancelled: Any) -> DesktopWindow:
    window, element = _target(desk, frame)
    if element.password:
        raise DesktopExecutionUnavailable(
            "the target is a password field; JARVIS does not type into it, the person does"
        )
    if frame.capability == "desktop_type" and desktop_element_role(element) not in DESKTOP_TEXT_ROLES:
        raise DesktopExecutionUnavailable("the target is not a text field")
    _not_cancelled(cancelled)
    desk.set_focus(element)
    if not desk.has_focus(element) or desk.foreground() != window.hwnd:
        raise DesktopExecutionUnavailable("the target did not take keyboard focus")
    return window


def _type(desk: Any, frame: DesktopRequestFrame, cancelled: Any, dispatch: _Dispatch) -> dict:
    window = _focused_field(desk, frame, cancelled)
    dispatch.started = True
    _injected(*desk.type_text(frame.resource.text))
    return {"window_id": window.window_id}


def _key(desk: Any, frame: DesktopRequestFrame, cancelled: Any, dispatch: _Dispatch) -> dict:
    window = _focused_field(desk, frame, cancelled)
    dispatch.started = True
    _injected(*desk.press_key(*DESKTOP_KEYS[frame.resource.key]))
    return {"window_id": window.window_id}


_OPERATIONS = {
    "desktop_window_list": _list,
    "desktop_window_inspect": _inspect,
    "desktop_app_launch": _launch,
    "desktop_window_focus": _focus,
    "desktop_click": _click,
    "desktop_type": _type,
    "desktop_key": _key,
}


# --------------------------------------------------------------------------
# The Windows backend - primitives over ctypes
# --------------------------------------------------------------------------

if sys.platform == "win32":
    from ctypes import wintypes as _wt

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _ole32 = ctypes.WinDLL("ole32")
    _oleaut32 = ctypes.WinDLL("oleaut32")

    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class _RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class _GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint16), ("d4", ctypes.c_ubyte * 8)]

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", _wt.DWORD),
                    ("dwFlags", _wt.DWORD), ("time", _wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", _wt.WORD), ("wScan", _wt.WORD), ("dwFlags", _wt.DWORD),
                    ("time", _wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", _wt.DWORD), ("wParamL", _wt.WORD), ("wParamH", _wt.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", _wt.DWORD), ("u", _INPUTUNION)]

    _WNDENUMPROC = ctypes.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)

    def _sig(dll: Any, name: str, restype: Any, *argtypes: Any) -> None:
        function = getattr(dll, name)
        function.restype, function.argtypes = restype, argtypes

    for _name, _res, _args in (
        ("EnumWindows", _wt.BOOL, (_WNDENUMPROC, _wt.LPARAM)),
        ("IsWindowVisible", _wt.BOOL, (_wt.HWND,)),
        ("GetWindowTextW", ctypes.c_int, (_wt.HWND, _wt.LPWSTR, ctypes.c_int)),
        ("GetClassNameW", ctypes.c_int, (_wt.HWND, _wt.LPWSTR, ctypes.c_int)),
        ("GetWindowThreadProcessId", _wt.DWORD, (_wt.HWND, ctypes.POINTER(_wt.DWORD))),
        ("IsIconic", _wt.BOOL, (_wt.HWND,)),
        ("GetForegroundWindow", _wt.HWND, ()),
        ("SetForegroundWindow", _wt.BOOL, (_wt.HWND,)),
        ("ShowWindow", _wt.BOOL, (_wt.HWND, ctypes.c_int)),
        ("GetAncestor", _wt.HWND, (_wt.HWND, _wt.UINT)),
        ("WindowFromPoint", _wt.HWND, (_POINT,)),
        ("GetCursorPos", _wt.BOOL, (ctypes.POINTER(_POINT),)),
        ("GetSystemMetrics", ctypes.c_int, (ctypes.c_int,)),
        ("SendInput", _wt.UINT, (_wt.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)),
        ("SetThreadDpiAwarenessContext", ctypes.c_void_p, (ctypes.c_void_p,)),
        ("WaitForInputIdle", _wt.DWORD, (_wt.HANDLE, _wt.DWORD)),
    ):
        _sig(_user32, _name, _res, *_args)
    _sig(_kernel32, "OpenProcess", _wt.HANDLE, _wt.DWORD, _wt.BOOL, _wt.DWORD)
    _sig(_kernel32, "CloseHandle", _wt.BOOL, _wt.HANDLE)
    _sig(_kernel32, "GetCurrentProcess", _wt.HANDLE)
    _sig(_kernel32, "QueryFullProcessImageNameW", _wt.BOOL, _wt.HANDLE, _wt.DWORD, _wt.LPWSTR, ctypes.POINTER(_wt.DWORD))
    _sig(_kernel32, "GetProcessTimes", _wt.BOOL, _wt.HANDLE, *([ctypes.POINTER(ctypes.c_uint64)] * 4))
    _sig(_advapi32, "OpenProcessToken", _wt.BOOL, _wt.HANDLE, _wt.DWORD, ctypes.POINTER(_wt.HANDLE))
    _sig(_advapi32, "GetTokenInformation", _wt.BOOL, _wt.HANDLE, ctypes.c_int, ctypes.c_void_p, _wt.DWORD, ctypes.POINTER(_wt.DWORD))
    _sig(_ole32, "CoInitializeEx", ctypes.c_long, ctypes.c_void_p, _wt.DWORD)
    _sig(_ole32, "CoUninitialize", None)
    _sig(_ole32, "CLSIDFromString", ctypes.c_long, _wt.LPCWSTR, ctypes.POINTER(_GUID))
    _sig(_ole32, "CoCreateInstance", ctypes.c_long, ctypes.POINTER(_GUID), ctypes.c_void_p, _wt.DWORD, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))
    _sig(_oleaut32, "SysStringLen", _wt.UINT, ctypes.c_void_p)
    _sig(_oleaut32, "SysFreeString", None, ctypes.c_void_p)

# UI Automation, from UIAutomationClient.h. Vtable slots count IUnknown's
# three methods first; each is checked against Win32's own answer for the
# same window in the one live validation (name, pid, handle, rectangle).
_CLSID_CUIAUTOMATION = "{FF48DBA4-60EF-4201-AA87-54103EEF594E}"
_IID_IUIAUTOMATION = "{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}"
_UIA_COMPARE_ELEMENTS = 3
_UIA_ELEMENT_FROM_HANDLE = 6
_UIA_GET_FOCUSED_ELEMENT = 8
_UIA_CONTROL_VIEW_CONDITION = 18
_ELEMENT_SET_FOCUS = 3
_ELEMENT_FIND_ALL = 6
_ELEMENT_CONTROL_TYPE = 21
_ELEMENT_NAME = 23
_ELEMENT_IS_ENABLED = 28
_ELEMENT_IS_PASSWORD = 35
_ELEMENT_IS_OFFSCREEN = 38
_ELEMENT_BOUNDING_RECTANGLE = 43
_ARRAY_LENGTH = 3
_ARRAY_GET_ELEMENT = 4
_TREE_SCOPE_DESCENDANTS = 4

_DPI_PER_MONITOR_AWARE_V2 = -4
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_ELEVATION = 20
_GA_ROOT = 2
_SW_RESTORE = 9
_INPUT_MOUSE, _INPUT_KEYBOARD = 0, 1
_MOUSE_MOVE, _MOUSE_LEFTDOWN, _MOUSE_LEFTUP = 0x0001, 0x0002, 0x0004
_MOUSE_VIRTUALDESK, _MOUSE_ABSOLUTE = 0x4000, 0x8000
_KEY_EXTENDED, _KEY_UP, _KEY_UNICODE = 0x0001, 0x0002, 0x0004


def _com(pointer: int, index: int, argtypes: tuple, *args: Any) -> None:
    """Call one COM method by vtable slot. A failing HRESULT raises OSError."""
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtable[index])(pointer, *args)


def _release(pointer: int) -> None:
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(pointer)


def _guid(text: str) -> Any:
    value = _GUID()
    if _ole32.CLSIDFromString(text, ctypes.byref(value)) < 0:
        raise OSError("bad GUID")
    return value


class Win32Desktop:
    """The real backend. One instance per operation, used as a context."""

    def __enter__(self) -> "Win32Desktop":
        if sys.platform != "win32":
            raise DesktopExecutionUnavailable("Windows Desktop control needs Windows")
        # Physical pixels everywhere - UI Automation rectangles, hit tests,
        # the cursor and SendInput - for this thread only.
        self._dpi = _user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(_DPI_PER_MONITOR_AWARE_V2))
        self._com_owned = _ole32.CoInitializeEx(None, 0) in (0, 1)
        self._held: list[int] = []
        self._uia = self._new(
            lambda out: _ole32.CoCreateInstance(
                ctypes.byref(_guid(_CLSID_CUIAUTOMATION)), None, 1,
                ctypes.byref(_guid(_IID_IUIAUTOMATION)), out,
            )
        )
        return self

    def __exit__(self, *exc: Any) -> None:
        for pointer in reversed(self._held):
            _release(pointer)
        self._held.clear()
        if self._com_owned:
            _ole32.CoUninitialize()
        _user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(self._dpi))

    def _new(self, make: Callable[[Any], Any]) -> int:
        out = ctypes.c_void_p()
        result = make(ctypes.byref(out))
        if isinstance(result, int) and result < 0:
            raise OSError(f"COM call failed: {result & 0xFFFFFFFF:#010x}")
        if not out.value:
            raise DesktopExecutionUnavailable("UI Automation returned no object")
        self._held.append(out.value)
        return out.value

    # -- windows and processes ------------------------------------------------

    @staticmethod
    def _process(pid: int) -> tuple[str, int]:
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return "", 0
        try:
            buffer, size = ctypes.create_unicode_buffer(1024), _wt.DWORD(1024)
            times = [ctypes.c_uint64() for _ in range(4)]
            if not _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return "", 0
            if not _kernel32.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
                return "", 0
            return buffer.value, times[0].value
        finally:
            _kernel32.CloseHandle(handle)

    def windows(self) -> list[DesktopWindow]:
        handles: list[int] = []
        _user32.EnumWindows(
            _WNDENUMPROC(lambda hwnd, _: bool(handles.append(hwnd) or True)), 0
        )
        foreground = _user32.GetForegroundWindow() or 0
        found = []
        for hwnd in handles:
            if not hwnd or not _user32.IsWindowVisible(hwnd):
                continue
            pid = _wt.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            image, created = self._process(pid.value)
            if not image:
                continue
            title, class_name = ctypes.create_unicode_buffer(512), ctypes.create_unicode_buffer(256)
            _user32.GetWindowTextW(hwnd, title, 512)
            _user32.GetClassNameW(hwnd, class_name, 256)
            found.append(
                DesktopWindow(
                    hwnd=hwnd, pid=pid.value, created=created, image=image,
                    title=title.value, class_name=class_name.value,
                    minimized=bool(_user32.IsIconic(hwnd)), foreground=hwnd == foreground,
                )
            )
        return found

    def foreground(self) -> int:
        return _user32.GetForegroundWindow() or 0

    def bring_to_front(self, hwnd: int) -> None:
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, _SW_RESTORE)
        _user32.SetForegroundWindow(hwnd)

    def root_at(self, x: int, y: int) -> int:
        hwnd = _user32.WindowFromPoint(_POINT(x, y))
        return (_user32.GetAncestor(hwnd, _GA_ROOT) or 0) if hwnd else 0

    def cursor(self) -> tuple[int, int]:
        point = _POINT()
        _user32.GetCursorPos(ctypes.byref(point))
        return point.x, point.y

    @staticmethod
    def _elevated(process: int) -> bool | None:
        token = _wt.HANDLE()
        if not _advapi32.OpenProcessToken(process, _TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            value, size = _wt.DWORD(), _wt.DWORD()
            ok = _advapi32.GetTokenInformation(token, _TOKEN_ELEVATION, ctypes.byref(value), 4, ctypes.byref(size))
            return bool(value.value) if ok else None
        finally:
            _kernel32.CloseHandle(token)

    def input_blocked(self, pid: int) -> bool:
        """Whether UIPI would drop our input to this process. Unknown counts
        as blocked: a token we may not even read is not one we outrank."""
        if self._elevated(_kernel32.GetCurrentProcess()):
            return False
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return True
        try:
            return self._elevated(handle) is not False
        finally:
            _kernel32.CloseHandle(handle)

    def launch(self, app: str) -> tuple[int, bool]:
        """Start the authorized image with NO arguments. A start that fails
        started nothing; a started program is reported even if not ready."""
        try:
            process = subprocess.Popen(
                [app.replace("/", "\\")],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise DesktopExecutionUnavailable(f"the application could not be started: {exc.strerror}") from exc
        ready = _user32.WaitForInputIdle(int(process._handle), DESKTOP_LAUNCH_READY_MS) == 0
        return process.pid, ready

    # -- UI Automation --------------------------------------------------------

    def _int(self, element: int, slot: int) -> int:
        out = ctypes.c_int()
        _com(element, slot, (ctypes.POINTER(ctypes.c_int),), ctypes.byref(out))
        return out.value

    def _text(self, element: int, slot: int) -> str:
        out = ctypes.c_void_p()
        _com(element, slot, (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(out))
        if not out.value:
            return ""
        try:
            return ctypes.wstring_at(out.value, _oleaut32.SysStringLen(out.value))
        finally:
            _oleaut32.SysFreeString(out.value)

    def elements(self, hwnd: int) -> list[DesktopElement]:
        """The window's control-view elements, in tree order. Role, name,
        rectangle and three flags - never a value."""
        uia = self._uia
        root = self._new(lambda out: _com(uia, _UIA_ELEMENT_FROM_HANDLE, (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)), hwnd, out))
        condition = self._new(lambda out: _com(uia, _UIA_CONTROL_VIEW_CONDITION, (ctypes.POINTER(ctypes.c_void_p),), out))
        found = self._new(
            lambda out: _com(root, _ELEMENT_FIND_ALL, (ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)), _TREE_SCOPE_DESCENDANTS, condition, out)
        )
        # ponytail: FindAll walks the whole subtree before the cap applies;
        # a cache request or a tree walker if huge windows prove slow.
        result = []
        for index in range(min(self._int(found, _ARRAY_LENGTH), DESKTOP_MAX_SCANNED)):
            element = self._new(lambda out: _com(found, _ARRAY_GET_ELEMENT, (ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)), index, out))
            rect = _RECT()
            _com(element, _ELEMENT_BOUNDING_RECTANGLE, (ctypes.POINTER(_RECT),), ctypes.byref(rect))
            result.append(
                DesktopElement(
                    control_type=self._int(element, _ELEMENT_CONTROL_TYPE),
                    name=self._text(element, _ELEMENT_NAME),
                    rect=(rect.left, rect.top, rect.right, rect.bottom),
                    enabled=bool(self._int(element, _ELEMENT_IS_ENABLED)),
                    password=bool(self._int(element, _ELEMENT_IS_PASSWORD)),
                    offscreen=bool(self._int(element, _ELEMENT_IS_OFFSCREEN)),
                    handle=element,
                )
            )
        return result

    def set_focus(self, element: DesktopElement) -> None:
        _com(element.handle, _ELEMENT_SET_FOCUS, ())

    def has_focus(self, element: DesktopElement) -> bool:
        uia = self._uia
        focused = self._new(lambda out: _com(uia, _UIA_GET_FOCUSED_ELEMENT, (ctypes.POINTER(ctypes.c_void_p),), out))
        same = ctypes.c_int()
        _com(uia, _UIA_COMPARE_ELEMENTS, (ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)), element.handle, focused, ctypes.byref(same))
        return bool(same.value)

    # -- input ----------------------------------------------------------------

    @staticmethod
    def _send(events: list[Any]) -> int:
        array = (_INPUT * len(events))(*events)
        return _user32.SendInput(len(events), array, ctypes.sizeof(_INPUT))

    @staticmethod
    def _mouse(x: int, y: int, flags: int) -> Any:
        left, top = _user32.GetSystemMetrics(76), _user32.GetSystemMetrics(77)
        width, height = _user32.GetSystemMetrics(78), _user32.GetSystemMetrics(79)
        dx = round((x - left) * 65535 / max(width - 1, 1))
        dy = round((y - top) * 65535 / max(height - 1, 1))
        flags |= _MOUSE_MOVE | _MOUSE_ABSOLUTE | _MOUSE_VIRTUALDESK
        return _INPUT(type=_INPUT_MOUSE, u=_INPUTUNION(mi=_MOUSEINPUT(dx, dy, 0, flags, 0, 0)))

    def move_cursor(self, x: int, y: int) -> bool:
        return self._send([self._mouse(x, y, 0)]) == 1

    def click(self, x: int, y: int) -> int:
        return self._send([self._mouse(x, y, 0), self._mouse(x, y, _MOUSE_LEFTDOWN), self._mouse(x, y, _MOUSE_LEFTUP)])

    @staticmethod
    def _key(vk: int, scan: int, flags: int) -> Any:
        return _INPUT(type=_INPUT_KEYBOARD, u=_INPUTUNION(ki=_KEYBDINPUT(vk, scan, flags, 0, 0)))

    def type_text(self, text: str) -> tuple[int, int]:
        units = text.encode("utf-16-le")
        events = []
        for offset in range(0, len(units), 2):
            unit = int.from_bytes(units[offset : offset + 2], "little")
            events += [self._key(0, unit, _KEY_UNICODE), self._key(0, unit, _KEY_UNICODE | _KEY_UP)]
        return self._send(events), len(events)

    def press_key(self, vk: int, extended: bool) -> tuple[int, int]:
        flags = _KEY_EXTENDED if extended else 0
        return self._send([self._key(vk, 0, flags), self._key(vk, 0, flags | _KEY_UP)]), 2


__all__ = ["DESKTOP_TIMEOUT_SECONDS", "Win32Desktop", "run_desktop_operation"]
