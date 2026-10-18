"""The Windows seam for Desktop basic: one validated operation, one backend.

``run_desktop_operation`` is the ORDER of checks, and the order is the safety
property: every refusal happens before anything is dispatched, and once an
action may have reached an application nothing short of a completed call is
treated as an answer. The backend (``Win32Desktop``) is primitives only -
enumerate, inspect, invoke, set a value - over the standard library's
``ctypes``: user32/kernel32 for windows and processes, and the operating
system's own UI Automation client (``IUIAutomation``) for the accessibility
tree and for every action.

TARGET-BOUND DISPATCH. No action travels through the global input stream.
A click is ``IUIAutomationInvokePattern::Invoke`` and typing is
``IUIAutomationValuePattern::SetValue``, each called on the pattern object of
the ONE element this operation just re-inspected and digest-verified. UI
Automation routes that call to that element's own provider, so which window
is in front, where the pointer is and what covers the element decide nothing:
there is no step at which Windows picks a recipient. There is no SendInput,
no synthesized mouse or keyboard event and no posted key message anywhere in
this module. An element that offers no such pattern is refused - never
clicked or typed into some other way.

OUTCOMES. ``DesktopExecutionUnavailable`` is a DEFINITE outcome: nothing was
dispatched, or - for a focus Windows declined - the observed end state is
known. ``DesktopOutcomeUnknown`` means the call was dispatched and did not
complete; it is never replayed.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from typing import Any, Callable

from jarvis.desktop import (
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
from jarvis.trusted_exec import NO_CONSOLE_WINDOW

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
    except Exception as exc:  # noqa: BLE001 - classified by whether the call left
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
    """The approved element in the approved window, still exactly as inspected.

    The element returned is the live UI Automation element of THIS
    inspection - the one whose rendering was just proven byte-identical to
    the approved one - and it is the object the action is then called on.
    """
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
    if desk.input_blocked(window.pid):
        # UIPI stands between a medium-integrity client and an elevated
        # application's UI; a call into it could only fail after dispatch.
        raise DesktopExecutionUnavailable(
            "the application runs elevated; JARVIS may not act on its UI"
        )
    return window, element


def _not_cancelled(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise DesktopExecutionUnavailable("the operation was cancelled before anything was dispatched")


def _still_the_approved_window(desk: Any, frame: DesktopRequestFrame, window: DesktopWindow) -> None:
    """Re-prove, AFTER the element and its pattern were acquired, that the
    handle they came from still belongs to the approved window.

    A window can close and Windows can hand its handle to another process
    while UI Automation is being walked; elements acquired through that
    handle would belong to the newcomer, and their rendering can look exactly
    like the approved one. So the live owner is judged again here - handle,
    pid, creation time, allowed image, refused classes, and the same image
    and class as approved - before dispatch starts. The pattern already in
    hand is what gets called; nothing is re-acquired after this check.
    """
    live = _window(desk, frame)
    if (live.image, live.class_name) != (window.image, window.class_name):
        raise DesktopExecutionUnavailable(
            "the window changed owner while its elements were being read; nothing was dispatched"
        )


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
        # another window in front (its foreground lock).
        raise DesktopExecutionUnavailable(
            "Windows kept another window in the foreground; the window may have "
            "been restored but it is not focused"
        )
    return {"window_id": window.window_id}


def _click(desk: Any, frame: DesktopRequestFrame, cancelled: Any, dispatch: _Dispatch) -> dict:
    window, element = _target(desk, frame)
    invoke = desk.pattern(element, "invoke")
    if invoke is None:
        raise DesktopExecutionUnavailable(
            "the element offers no UI Automation Invoke, so it cannot be clicked "
            "by a call bound to it; nothing was clicked"
        )
    _still_the_approved_window(desk, frame, window)
    _not_cancelled(cancelled)
    dispatch.started = True
    desk.invoke(invoke)
    return {"window_id": window.window_id}


def _type(desk: Any, frame: DesktopRequestFrame, cancelled: Any, dispatch: _Dispatch) -> dict:
    window, element = _target(desk, frame)
    if element.password:
        raise DesktopExecutionUnavailable(
            "the target is a password field; JARVIS does not type into it, the person does"
        )
    if desktop_element_role(element) not in DESKTOP_TEXT_ROLES:
        raise DesktopExecutionUnavailable("the target is not a text field")
    value = desk.pattern(element, "value")
    # Only the read-only FLAG is asked for; the field's value is never read.
    if value is None or desk.read_only(value):
        raise DesktopExecutionUnavailable(
            "the field offers no writable UI Automation value, so it cannot be "
            "typed into by a call bound to it; nothing was typed"
        )
    _still_the_approved_window(desk, frame, window)
    _not_cancelled(cancelled)
    dispatch.started = True
    desk.set_value(value, frame.resource.text)
    return {"window_id": window.window_id}


_OPERATIONS = {
    "desktop_window_list": _list,
    "desktop_window_inspect": _inspect,
    "desktop_app_launch": _launch,
    "desktop_window_focus": _focus,
    "desktop_click": _click,
    "desktop_type": _type,
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

    class _RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class _GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint16), ("d4", ctypes.c_ubyte * 8)]

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
    _sig(_oleaut32, "SysAllocString", ctypes.c_void_p, _wt.LPCWSTR)
    _sig(_oleaut32, "SysStringLen", _wt.UINT, ctypes.c_void_p)
    _sig(_oleaut32, "SysFreeString", None, ctypes.c_void_p)

# UI Automation, from UIAutomationClient.h. Vtable slots count IUnknown's
# three methods first; the element slots were checked against Win32's own
# answers for the same window in the live validation.
_CLSID_CUIAUTOMATION = "{FF48DBA4-60EF-4201-AA87-54103EEF594E}"
_IID_IUIAUTOMATION = "{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}"
_UIA_ELEMENT_FROM_HANDLE = 6
_UIA_CONTROL_VIEW_CONDITION = 18
_ELEMENT_FIND_ALL = 6
_ELEMENT_GET_CURRENT_PATTERN_AS = 14
_ELEMENT_CONTROL_TYPE = 21
_ELEMENT_NAME = 23
_ELEMENT_IS_ENABLED = 28
_ELEMENT_IS_PASSWORD = 35
_ELEMENT_IS_OFFSCREEN = 38
_ELEMENT_BOUNDING_RECTANGLE = 43
_ARRAY_LENGTH = 3
_ARRAY_GET_ELEMENT = 4
_TREE_SCOPE_DESCENDANTS = 4
#: The two patterns an action may be called through: (pattern id, IID).
_PATTERNS = {
    "invoke": (10000, "{FB377FBE-8EA6-46D5-9C73-6499642D3059}"),
    "value": (10002, "{A94CD8B1-0844-4CD6-9D2D-640537AB39E9}"),
}
_INVOKE = 3
_VALUE_SET_VALUE = 3
_VALUE_IS_READ_ONLY = 5  # slot 4, get_CurrentValue, is never called

_DPI_PER_MONITOR_AWARE_V2 = -4
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_ELEVATION = 20
_SW_RESTORE = 9


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
        # Physical pixels for UI Automation rectangles, for this thread only,
        # so an inspection renders the same bytes however the process scales.
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
        """Whether UIPI stands between us and this process. Unknown counts as
        blocked: a token we may not even read is not one we outrank."""
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
                creationflags=NO_CONSOLE_WINDOW,
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

    def pattern(self, element: DesktopElement, kind: str) -> int | None:
        """This element's own Invoke or Value pattern object, or ``None`` when
        it offers none. Asking changes nothing in the application."""
        pattern_id, iid = _PATTERNS[kind]
        out = ctypes.c_void_p()
        _com(
            element.handle, _ELEMENT_GET_CURRENT_PATTERN_AS,
            (ctypes.c_int, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)),
            pattern_id, ctypes.byref(_guid(iid)), ctypes.byref(out),
        )
        if not out.value:
            return None
        self._held.append(out.value)
        return out.value

    def read_only(self, pattern: int) -> bool:
        return bool(self._int(pattern, _VALUE_IS_READ_ONLY))

    def invoke(self, pattern: int) -> None:
        _com(pattern, _INVOKE, ())

    def set_value(self, pattern: int, text: str) -> None:
        value = _oleaut32.SysAllocString(text)
        if not value:
            raise MemoryError("no string for the value")
        try:
            _com(pattern, _VALUE_SET_VALUE, (ctypes.c_void_p,), value)
        finally:
            _oleaut32.SysFreeString(value)


__all__ = ["DESKTOP_TIMEOUT_SECONDS", "Win32Desktop", "run_desktop_operation"]
