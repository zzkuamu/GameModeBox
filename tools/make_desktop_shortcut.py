# -*- coding: utf-8 -*-
"""在桌面创建「游戏模式盒.lnk」（纯 ctypes 走 Windows Shell COM）。

为什么不走 PowerShell New-Object -ComObject：本机沙箱拦它（实测），
而 ctypes.WinDLL('ole32').CoCreateInstance 不受限（2026-09-27 实测通过）。

要点（都来自实测踩坑）：
- GUID 结构体按 time_low/mid/hi_version 字符串分段手工填，不能 from_buffer_copy
  文本 hex（字节序会错）。
- vtable 调用必须显式声明参数类型；byref() 返回 CArgObject，没有 from_param，
  用 type(arg) 推导会 TypeError。
- 验证必须 Load + GetPath 读回比对，不能只看 Save 返回 0。
- 桌面路径从注册表读，不写死用户名（脚本要能上 GitHub）。
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys

import winreg

ole32 = ctypes.WinDLL("ole32", use_last_error=True)

CLSID_ShellLink = "00021401-0000-0000-C000-000000000046"
IID_IShellLinkW = "000214F9-0000-0000-C000-000000000046"
IID_IPersistFile = "0000010B-0000-0000-C000-000000000046"


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(s: str) -> GUID:
    a, b, c, d1, d2 = s.split("-")
    g = GUID(int(a, 16), int(b, 16), int(c, 16))
    for i, v in enumerate(bytes.fromhex(d1 + d2)):
        g.Data4[i] = v
    return g


def _method(iface: int, idx: int, restype, *argtypes):
    """按 vtable 索引取函数指针（显式签名，不猜参数类型）。"""
    vtbl = ctypes.POINTER(ctypes.c_void_p).from_address(iface)
    return ctypes.cast(
        vtbl[idx],
        ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes),
    )


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    target = os.path.join(root, "main.pyw")
    icon = os.path.join(root, "assets", "icon.ico")
    workdir = root

    # 桌面路径从注册表拿（避免硬编码用户名）
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        0, winreg.KEY_READ,
    ) as k:
        desktop, _ = winreg.QueryValueEx(k, "Desktop")
    lnk_path = os.path.join(desktop, "游戏模式盒.lnk")

    # CoCreateInstance -> IShellLinkW
    sl = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(
        ctypes.byref(_guid(CLSID_ShellLink)), None, 1,  # CLSCTX_INPROC_SERVER
        ctypes.byref(_guid(IID_IShellLinkW)), ctypes.byref(sl))
    if hr != 0 or not sl.value:
        print(f"CoCreateInstance failed: 0x{hr & 0xFFFFFFFF:08X}")
        return 1

    HRESULT = ctypes.c_long
    SetPath = _method(sl.value, 20, HRESULT, ctypes.c_wchar_p)
    SetWorkingDirectory = _method(sl.value, 9, HRESULT, ctypes.c_wchar_p)
    SetIconLocation = _method(sl.value, 17, HRESULT, ctypes.c_wchar_p, ctypes.c_int)
    SetDescription = _method(sl.value, 7, HRESULT, ctypes.c_wchar_p)
    GetPath = _method(sl.value, 3, HRESULT, ctypes.c_wchar_p, ctypes.c_uint,
                      ctypes.c_void_p, ctypes.c_uint)

    SetPath(sl.value, target)
    SetWorkingDirectory(sl.value, workdir)
    if os.path.exists(icon):
        SetIconLocation(sl.value, icon, 0)
    SetDescription(sl.value, "游戏模式盒 - 三角洲/暗区突围/无畏契约 一键游戏模式")

    # QueryInterface(IPersistFile) -> Save
    # 注意：_method 已隐式带 this(c_void_p)，参数表里不要再写一次
    pf = ctypes.c_void_p()
    qi = _method(sl.value, 0, HRESULT,
                 ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
    hr = qi(sl.value, ctypes.byref(_guid(IID_IPersistFile)), ctypes.byref(pf))
    if hr != 0 or not pf.value:
        print(f"QueryInterface(IPersistFile) failed: 0x{hr & 0xFFFFFFFF:08X}")
        return 1

    Save = _method(pf.value, 6, HRESULT, ctypes.c_wchar_p, wt.BOOL)
    Load = _method(pf.value, 5, HRESULT, ctypes.c_wchar_p, wt.DWORD)
    hr = Save(pf.value, lnk_path, True)
    if hr != 0:
        print(f"Save failed: 0x{hr & 0xFFFFFFFF:08X}")
        return 1

    # 验证：Load 回来 + GetPath 读回比对（只看 Save 返回 0 不算数）
    hr = Load(pf.value, lnk_path, 0)
    buf = ctypes.create_unicode_buffer(260)
    GetPath(sl.value, buf, 260, None, 0)
    if os.path.normcase(buf.value) != os.path.normcase(target):
        print(f"VERIFY FAIL: readback={buf.value!r} expected={target!r}")
        return 1
    print(f"OK: {lnk_path}")
    print(f"  -> {buf.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
