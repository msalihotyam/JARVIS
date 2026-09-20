"""A child process whose lifetime the OPERATING SYSTEM binds to this one.

WHY THIS EXISTS. JARVIS's network policy over a browser page is a JARVIS
object: a socket, a thread, a method set. Every one of those dies when the
JARVIS process dies, and Chromium detaches interception when its DevTools
client disappears. So a browser launched with an ordinary ``Popen`` and then
orphaned by a crash is a loaded, untrusted page left ONLINE with nothing
judging it - which is exactly the state the whole browser boundary exists to
prevent (DB-002B-01).

An orderly shutdown could close that page itself, and does. The problem is the
disorderly ones: an uncaught exception, ``TerminateProcess``, Task Manager,
a power-management kill, a hard fault in Qt. None of those run ``atexit``,
none of them run a ``finally``, none of them get to send a protocol message.
A guarantee that depends on JARVIS still being able to execute code is not a
guarantee against JARVIS not existing.

WHAT IS USED INSTEAD is a Windows Job Object with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. The rule it expresses is enforced by
the kernel: when the last handle to the job closes, every process still in the
job is terminated. JARVIS holds the only handle, and the OS closes every
handle a process owns when that process ends - however it ends. So the browser
tree cannot outlive JARVIS, and nothing in this file has to run for that to be
true.

THE RACE THIS AVOIDS. Spawning normally and assigning afterwards leaves a
window in which the browser is running and unowned; a browser that finished
starting inside that window would never be in the job at all. The process is
therefore created SUSPENDED, assigned, VERIFIED to be in the job, and only
then resumed. It has not executed an instruction before it is owned.

THE WHOLE TREE, not one process. A job contains the processes assigned to it
and everything they go on to create. Breakaway is impossible unless the job
permits it: neither ``JOB_OBJECT_LIMIT_BREAKAWAY_OK`` nor
``JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK`` is set here, so Chromium's renderer,
GPU, network and utility processes - and their descendants - are contained
whether or not they ask to be.

ponytail: ctypes against kernel32 rather than a dependency. This is four
documented calls; a package to make them look nicer would be a supply-chain
entry for the privilege.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "OwnedProcess",
    "ProcessOwnershipError",
    "launch_owned",
    "listening_pid",
    "ownership_is_available",
]


class ProcessOwnershipError(RuntimeError):
    """A child could not be created OWNED, so it was not created at all."""


#: ``CreateProcessW``: start the primary thread suspended.
_CREATE_SUSPENDED = 0x00000004
#: ``SetInformationJobObject``: ``JobObjectExtendedLimitInformation``.
_JOB_EXTENDED_LIMIT = 9
#: The one limit this build sets, and the entire point of the file.
_JOB_KILL_ON_CLOSE = 0x00002000
#: What is deliberately NOT set. Named so a reader can see the absence.
_JOB_BREAKAWAY_OK = 0x00000800
_JOB_SILENT_BREAKAWAY_OK = 0x00001000
#: ``GetHandleInformation``.
_HANDLE_FLAG_INHERIT = 0x00000001
#: ``GetExitCodeProcess`` while the process is still running.
_STILL_ACTIVE = 259
#: ``OpenProcess``: enough to ask what a process is, and nothing more.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
#: ``GetExtendedTcpTable``: listening sockets with their owning process.
_TCP_TABLE_OWNER_PID_LISTENER = 3
_AF_INET = 2
_ERROR_INSUFFICIENT_BUFFER = 122
_RESUME_FAILED = 0xFFFFFFFF

_IS_WINDOWS = sys.platform == "win32"


def ownership_is_available() -> bool:
    """Whether this platform can bind a child's lifetime at the OS level."""
    return _IS_WINDOWS


if _IS_WINDOWS:  # pragma: no branch - the product is a Windows desktop app
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    # SIGNATURES, NOT DEFAULTS. ctypes defaults a return value to C ``int``,
    # which TRUNCATES a 64-bit HANDLE to 32 bits - the resulting handle is
    # silently wrong and every call on it fails in a way that looks like a
    # permissions problem. Every function used here is declared.
    _kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD
    )
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.IsProcessInJob.argtypes = (
        wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)
    )
    _kernel32.IsProcessInJob.restype = wintypes.BOOL
    _kernel32.CreateProcessW.argtypes = (
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.LPVOID, wintypes.LPVOID,
        wintypes.BOOL, wintypes.DWORD, wintypes.LPVOID, wintypes.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOW), ctypes.POINTER(_PROCESS_INFORMATION),
    )
    _kernel32.CreateProcessW.restype = wintypes.BOOL
    _kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
    _kernel32.ResumeThread.restype = wintypes.DWORD
    _kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
    )
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.GetHandleInformation.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
    )
    _kernel32.GetHandleInformation.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL


    class _MIB_TCPROW_OWNER_PID(ctypes.Structure):
        _fields_ = [
            ("dwState", wintypes.DWORD),
            ("dwLocalAddr", wintypes.DWORD),
            ("dwLocalPort", wintypes.DWORD),
            ("dwRemoteAddr", wintypes.DWORD),
            ("dwRemotePort", wintypes.DWORD),
            ("dwOwningPid", wintypes.DWORD),
        ]

    _iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
    _iphlpapi.GetExtendedTcpTable.argtypes = (
        wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
        wintypes.ULONG, ctypes.c_int, wintypes.ULONG,
    )
    _iphlpapi.GetExtendedTcpTable.restype = wintypes.DWORD


def listening_pid(port: int) -> int | None:
    """Which process is LISTENING on ``port``, or ``None`` if nothing is.

    Asked because "JARVIS started a browser and it is still running" and
    "the thing answering on JARVIS's debugging port is that browser" are
    different claims, and only the second one justifies driving it. Without
    this, a browser of ours that failed to bind the port while something else
    held it would still look like ownership of that endpoint.
    """
    if not _IS_WINDOWS:  # pragma: no cover - the caller is Windows-only
        return None
    import ctypes
    from ctypes import wintypes

    size = wintypes.DWORD(0)
    _iphlpapi.GetExtendedTcpTable(
        None, ctypes.byref(size), False, _AF_INET, _TCP_TABLE_OWNER_PID_LISTENER, 0
    )
    if not size.value:  # pragma: no cover - there is always a listener
        return None
    buffer = ctypes.create_string_buffer(size.value)
    if _iphlpapi.GetExtendedTcpTable(
        buffer, ctypes.byref(size), False, _AF_INET, _TCP_TABLE_OWNER_PID_LISTENER, 0
    ):  # pragma: no cover - the size was just asked for
        return None
    count = wintypes.DWORD.from_buffer(buffer).value
    rows = (_MIB_TCPROW_OWNER_PID * count).from_buffer(
        buffer, ctypes.sizeof(wintypes.DWORD)
    )
    for row in rows:
        # The port sits in the low two bytes in NETWORK order.
        listening = ((row.dwLocalPort & 0xFF) << 8) | ((row.dwLocalPort >> 8) & 0xFF)
        if listening == port:
            return int(row.dwOwningPid)
    return None


def _last_error() -> str:
    import ctypes

    return f"Windows error {ctypes.get_last_error()}"


@dataclass
class OwnedProcess:
    """A child process the OS will terminate when JARVIS stops existing.

    Holding this object is what keeps the child alive: it owns the only handle
    to the job, and the job kills its contents when that handle goes. Nothing
    else in JARVIS needs to remember to do anything.
    """

    pid: int
    #: The job whose closing terminates the tree. THE handle, singular and
    #: non-inheritable, so no other process can keep the job open.
    job: int
    #: Kept for liveness only. Closing it does not affect the child.
    process: int
    _closed: bool = False

    def alive(self) -> bool:
        """Whether the child process is still running."""
        if self._closed:
            return False
        import ctypes
        from ctypes import wintypes

        code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(
            wintypes.HANDLE(self.process), ctypes.byref(code)
        ):
            return False
        return code.value == _STILL_ACTIVE

    def contained(self) -> bool:
        """Whether the child is still IN the job that owns its lifetime.

        Asked before a browser is reused, because "a process I started" and
        "a process the OS will kill with me" are not the same claim, and only
        the second one is worth anything here.
        """
        if self._closed:
            return False
        import ctypes
        from ctypes import wintypes

        inside = wintypes.BOOL()
        if not _kernel32.IsProcessInJob(
            wintypes.HANDLE(self.process), wintypes.HANDLE(self.job), ctypes.byref(inside)
        ):
            return False
        return bool(inside.value)

    def contains(self, pid: int | None) -> bool:
        """Whether ``pid`` is another process inside THIS job.

        A browser is a tree, and the process holding the debugging port need
        not be the one that was assigned - so ownership of an endpoint is
        asked of the job, not of a remembered pid.
        """
        if self._closed or pid is None:
            return False
        import ctypes
        from ctypes import wintypes

        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            inside = wintypes.BOOL()
            if not _kernel32.IsProcessInJob(
                wintypes.HANDLE(handle), wintypes.HANDLE(self.job), ctypes.byref(inside)
            ):  # pragma: no cover - a live handle answers
                return False
            return bool(inside.value)
        finally:
            _kernel32.CloseHandle(wintypes.HANDLE(handle))

    def job_is_inheritable(self) -> bool:
        """Whether the job handle could be inherited by a child. It must not.

        An inherited job handle would mean the browser itself keeps the job
        open, and then JARVIS dying would close only one of two handles and
        terminate nothing.
        """
        import ctypes
        from ctypes import wintypes

        flags = wintypes.DWORD()
        if not _kernel32.GetHandleInformation(
            wintypes.HANDLE(self.job), ctypes.byref(flags)
        ):  # pragma: no cover - a live handle answers
            return True
        return bool(flags.value & _HANDLE_FLAG_INHERIT)

    def limit_flags(self) -> int:
        """The limits the KERNEL says this job has, read back from it.

        Asked by the tests rather than trusted from the struct that was
        written: "kill on close is set and breakaway is not" is a claim about
        the job, and the job is where it can be checked.
        """
        import ctypes
        from ctypes import wintypes

        limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        returned = wintypes.DWORD()
        if not _kernel32.QueryInformationJobObject(
            wintypes.HANDLE(self.job),
            _JOB_EXTENDED_LIMIT,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
            ctypes.byref(returned),
        ):  # pragma: no cover - a live job answers
            return 0
        return int(limits.BasicLimitInformation.LimitFlags)

    def close(self) -> None:
        """Release the job, which TERMINATES the whole tree, then the handles.

        The deliberate order: the job handle is what kills, so it goes first
        and the process handle is only let go afterwards. Idempotent, because
        a failure path may reach it after a normal one already has.
        """
        if self._closed:
            return
        self._closed = True
        from ctypes import wintypes

        _kernel32.CloseHandle(wintypes.HANDLE(self.job))
        _kernel32.CloseHandle(wintypes.HANDLE(self.process))


def _assign_to_job(job: int, process: int) -> bool:
    from ctypes import wintypes

    return bool(
        _kernel32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(process))
    )


def launch_owned(
    argv: Sequence[str],
    *,
    assign: Any = _assign_to_job if _IS_WINDOWS else None,
) -> OwnedProcess:
    """Start ``argv`` so that the OS will kill it when this process ends.

    The ordering is the security property and it is not negotiable:

      1. create the job, with kill-on-close and no breakaway - so a failure
         here has spawned nothing at all;
      2. ``CreateProcessW`` with ``CREATE_SUSPENDED`` - the child exists but
         has not executed an instruction;
      3. ``AssignProcessToJobObject``;
      4. ``IsProcessInJob`` - VERIFIED, because an API that returned success
         is not the same as a process that is in the job;
      5. ``ResumeThread``.

    Any failure terminates the child and closes every handle, and raises.
    There is no unmanaged fallback: a browser that cannot be owned is a
    browser this product does not start.

    ``assign`` exists so a test can make step 3 fail and watch step 4 refuse
    to paper over it.
    """
    if not _IS_WINDOWS:
        raise ProcessOwnershipError(
            "this build binds a child browser's lifetime with a Windows job "
            "object, and this platform has none, so no browser is started "
            "rather than one that could outlive JARVIS"
        )
    import ctypes
    from ctypes import wintypes

    if not argv:
        raise ProcessOwnershipError("no executable was named")
    command = subprocess.list2cmdline(list(argv))

    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ProcessOwnershipError(f"the lifetime job could not be created: {_last_error()}")
    limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    # KILL ON CLOSE, and nothing else. Breakaway is left unset, which is what
    # keeps the renderer and every other Chromium child inside the job.
    limits.BasicLimitInformation.LimitFlags = _JOB_KILL_ON_CLOSE
    if not _kernel32.SetInformationJobObject(
        wintypes.HANDLE(job), _JOB_EXTENDED_LIMIT, ctypes.byref(limits), ctypes.sizeof(limits)
    ):
        error = _last_error()
        _kernel32.CloseHandle(wintypes.HANDLE(job))
        raise ProcessOwnershipError(f"the lifetime job could not be limited: {error}")

    startup = _STARTUPINFOW()
    startup.cb = ctypes.sizeof(startup)
    information = _PROCESS_INFORMATION()
    # NOTHING IS INHERITED. The job handle must belong to this process alone,
    # or closing it would not be the last close and would kill nothing.
    created = _kernel32.CreateProcessW(
        str(argv[0]),
        ctypes.create_unicode_buffer(command),
        None,
        None,
        False,
        _CREATE_SUSPENDED,
        None,
        None,
        ctypes.byref(startup),
        ctypes.byref(information),
    )
    if not created:
        error = _last_error()
        _kernel32.CloseHandle(wintypes.HANDLE(job))
        raise ProcessOwnershipError(f"the browser process could not be created: {error}")

    owned = OwnedProcess(
        pid=int(information.dwProcessId),
        job=int(job),
        process=int(information.hProcess),
    )

    def fail(reason: str) -> None:
        """Kill the suspended child and let go of everything. Always raises."""
        _kernel32.TerminateProcess(wintypes.HANDLE(information.hProcess), 1)
        _kernel32.CloseHandle(wintypes.HANDLE(information.hThread))
        owned.close()
        raise ProcessOwnershipError(reason)

    if not assign(job, int(information.hProcess)):
        fail(f"the browser could not be bound to JARVIS's lifetime: {_last_error()}")
    # VERIFIED, not assumed. This is the line that makes the guarantee a fact
    # about the kernel's bookkeeping rather than about a return value.
    if not owned.contained():
        fail(
            "the browser was not in the job that binds its lifetime to "
            "JARVIS's, so it was terminated rather than run unowned"
        )
    if owned.job_is_inheritable():  # pragma: no cover - a fresh job is not
        fail("the lifetime job handle is inheritable, which would not bind anything")
    if _kernel32.ResumeThread(wintypes.HANDLE(information.hThread)) == _RESUME_FAILED:
        fail(f"the browser could not be started: {_last_error()}")
    _kernel32.CloseHandle(wintypes.HANDLE(information.hThread))
    return owned
