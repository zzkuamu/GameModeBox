"""Win32 / NT 底层调用封装（纯 ctypes，无第三方依赖）。

设计原则：
- 每个函数都是"薄封装"，不缓存状态，每次直读系统 —— 测试可以独立回读验证。
- 打开句柄一律 try/finally 关闭；失败返回 None/False，不抛到 UI 层。
- 所有可能被 64 位参数影响的调用显式声明 argtypes/restype。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
psapi = ctypes.WinDLL("psapi.dll", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll.dll", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

# ---------------------------------------------------------------- 常量
TH32CS_SNAPPROCESS = 0x00000002

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_SET_INFORMATION = 0x0200
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SUSPEND_RESUME = 0x0800

PRIORITY_CLASSES = {
    "idle": 0x00000040,
    "below_normal": 0x00004000,
    "normal": 0x00000020,
    "above_normal": 0x00008000,
    "high": 0x00000080,
    "realtime": 0x00000100,
}
PRIORITY_NAMES = {v: k for k, v in PRIORITY_CLASSES.items()}

# ---------------------------------------------------------------- 结构体


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


# ---------------------------------------------------------------- 声明
kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wt.BOOL
kernel32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.GetCurrentProcess.argtypes = []
kernel32.GetCurrentProcess.restype = wt.HANDLE
kernel32.GetPriorityClass.argtypes = [wt.HANDLE]
kernel32.GetPriorityClass.restype = wt.DWORD
kernel32.SetPriorityClass.argtypes = [wt.HANDLE, wt.DWORD]
kernel32.SetPriorityClass.restype = wt.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wt.HANDLE, wt.DWORD, ctypes.c_wchar_p, ctypes.POINTER(wt.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL

user32.EnumWindows.argtypes = [ctypes.c_void_p, wt.LPARAM]
user32.EnumWindows.restype = wt.BOOL
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
# 64 位下必须声明 restype=HWND，否则句柄被按 32 位截断
user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
user32.FindWindowW.restype = wt.HWND
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_long]
user32.PostMessageW.restype = wt.BOOL

psapi.EmptyWorkingSet.argtypes = [wt.HANDLE]
psapi.EmptyWorkingSet.restype = wt.BOOL
psapi.GetProcessMemoryInfo.argtypes = [
    wt.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wt.DWORD,
]
psapi.GetProcessMemoryInfo.restype = wt.BOOL

ntdll.NtSuspendProcess.argtypes = [wt.HANDLE]
ntdll.NtSuspendProcess.restype = ctypes.c_long  # NTSTATUS
ntdll.NtResumeProcess.argtypes = [wt.HANDLE]
ntdll.NtResumeProcess.restype = ctypes.c_long


# ---------------------------------------------------------------- 进程


def list_processes() -> dict[int, str]:
    """{pid: exe文件名}，全量快照。"""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        return {}
    out: dict[int, str] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out[int(entry.th32ProcessID)] = entry.szExeFile
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return out


def image_path(pid: int) -> str | None:
    """进程映像完整路径；拿不到返回 None。"""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        size = wt.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(h)


def get_priority(pid: int) -> str | None:
    """读取进程优先级类名（'high' 等）；失败 None。"""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        val = kernel32.GetPriorityClass(h)
        return PRIORITY_NAMES.get(val)
    finally:
        kernel32.CloseHandle(h)


def set_priority(pid: int, cls: str) -> tuple[bool, str]:
    """设置优先级并回读实际生效值。返回 (是否达到目标, 实际优先级)。"""
    want = PRIORITY_CLASSES.get(cls)
    if want is None:
        return False, f"未知优先级: {cls}"
    h = kernel32.OpenProcess(
        PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not h:
        return False, "打开进程失败(权限不足或进程已退出)"
    try:
        if not kernel32.SetPriorityClass(h, want):
            return False, "SetPriorityClass 调用失败"
        actual = kernel32.GetPriorityClass(h)
        name = PRIORITY_NAMES.get(actual, str(actual))
        return actual == want, name
    finally:
        kernel32.CloseHandle(h)


def suspend_process(pid: int) -> bool:
    h = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
    if not h:
        return False
    try:
        return ntdll.NtSuspendProcess(h) == 0
    finally:
        kernel32.CloseHandle(h)


def resume_process(pid: int) -> bool:
    h = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
    if not h:
        return False
    try:
        return ntdll.NtResumeProcess(h) == 0
    finally:
        kernel32.CloseHandle(h)


def empty_working_set(pid: int) -> bool:
    h = kernel32.OpenProcess(
        PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION
        | PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        pid,
    )
    if not h:
        return False
    try:
        return bool(psapi.EmptyWorkingSet(h))
    finally:
        kernel32.CloseHandle(h)


def working_set_bytes(pid: int | None = None) -> int | None:
    """进程当前工作集字节数；pid=None 表示自身。QUERY_LIMITED 失败时回退完整权限。"""
    if pid is None:
        h, close = kernel32.GetCurrentProcess(), False
    else:
        h, close = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid), True
    if not h:
        return None
    try:
        def _read(handle) -> int | None:
            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
            return None

        val = _read(h)
        if val is not None or not close:
            return val
        # 回退：PROCESS_QUERY_INFORMATION | PROCESS_VM_READ（旧文档要求的权限组合）
        kernel32.CloseHandle(h)
        h = kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION | 0x0010, False, pid)  # 0x10 = PROCESS_VM_READ
        if not h:
            return None
        return _read(h)
    finally:
        if close and h:
            kernel32.CloseHandle(h)


# ---------------------------------------------------------------- 窗口


def list_visible_windows() -> list[tuple[int, str]]:
    """[(pid, 窗口标题)]，仅可见顶层窗口。"""
    out: list[tuple[int, str]] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def _cb(hwnd, _lparam):
        try:
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    if buf.value:
                        pid = wt.DWORD(0)
                        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                        if pid.value:
                            out.append((int(pid.value), buf.value))
        except Exception:
            pass
        return True

    user32.EnumWindows(ctypes.cast(WNDENUMPROC(_cb), ctypes.c_void_p), 0)
    return out


# ---------------------------------------------------------------- 权限


def is_admin() -> bool:
    try:
        return bool(shell32.IsUserAnAdmin())
    except Exception:
        return False
