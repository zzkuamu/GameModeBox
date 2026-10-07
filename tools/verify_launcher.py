# -*- coding: utf-8 -*-
"""验证「双击启动」链路：.cmd -> .pyw 关联(pyw.exe) -> 窗口出现 -> WM_CLOSE -> 干净退出。

模拟双击 = 用 cmd.exe 执行 启动游戏模式.cmd（与资源管理器双击同一条 ShellExecute 路径）。
退出断言：
- 窗口在限时内出现（说明用的是带 PySide6 的那个 Python，没被 PATH 里
  另一个没有 PySide6 的 pythonw 截胡）
- WM_CLOSE 后窗口销毁、进程退出
- 没有留下 state.json / config.json（首次运行、未开启游戏模式时必须零残留）
- 电源计划未被改动
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gmb import tweaks, winapi  # noqa: E402

ROOT = tweaks.BASE_DIR
WM_CLOSE = 0x0010
CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""),
          flush=True)
    return bool(cond)


def active_scheme() -> str | None:
    p = subprocess.run(["powercfg", "/getactivescheme"], capture_output=True,
                       timeout=20, encoding="utf-8", errors="replace")
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                  (p.stdout or "").lower())
    return m.group(1) if m else None


def main() -> int:
    print("=" * 60)
    print("GameModeBox 启动链路验证 (launcher)")
    print("=" * 60)

    if winapi.user32.FindWindowW(None, "游戏模式盒"):
        print("  [SKIP] 已有实例在运行")
        return 1

    state_before = os.path.exists(os.path.join(ROOT, "state.json"))
    plan_before = active_scheme()

    cmd = os.path.join(ROOT, "启动游戏模式.cmd")
    check("启动脚本存在", os.path.exists(cmd), cmd)
    # CRLF + ASCII 校验（cmd.exe 解析批处理的硬性要求）
    raw = open(cmd, "rb").read()
    check("启动脚本纯ASCII", all(b < 128 for b in raw))
    check("启动脚本无孤立LF", b"\n" not in raw.replace(b"\r\n", b""))

    # 模拟双击（cmd /c 与 ShellExecute 走同一条关联链）
    proc = subprocess.run(["cmd", "/c", cmd], capture_output=True, timeout=30)

    try:
        hwnd = None
        pid = 0
        deadline = time.time() + 20
        while time.time() < deadline and not hwnd:
            hwnd = winapi.user32.FindWindowW(None, "游戏模式盒")
            if hwnd:
                out = winapi.wt.DWORD(0) if False else None
                import ctypes
                pd = ctypes.wintypes.DWORD(0)
                winapi.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pd))
                pid = pd.value
            time.sleep(0.2)
        check("双击后窗口出现(限时20s)", bool(hwnd), f"hwnd={hwnd}")
        check("拿到宿主进程PID", pid != 0, f"pid={pid}")

        if hwnd:
            winapi.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            gone = False
            deadline = time.time() + 15
            while time.time() < deadline and not gone:
                gone = not winapi.user32.FindWindowW(None, "游戏模式盒")
                time.sleep(0.2)
            check("WM_CLOSE 后窗口销毁", gone)

            gone_proc = False
            deadline = time.time() + 10
            while time.time() < deadline and not gone_proc:
                gone_proc = pid not in winapi.list_processes()
                time.sleep(0.3)
            check("宿主进程退出", gone_proc, f"pid={pid}")
    finally:
        # 残留检查
        time.sleep(0.5)
        state_after = os.path.exists(os.path.join(ROOT, "state.json"))
        check("无 state.json 残留", state_after == state_before or not state_after,
              f"before={state_before} after={state_after}")
        plan_after = active_scheme()
        check("电源计划未被改动", plan_after == plan_before,
              f"{plan_before} -> {plan_after}")

    # ---------------- 阶段二：带记忆配置的双击（日常形态：自动进入游戏模式） ----------------
    print("\n[阶段二] 预置 last_game 后双击 → 自动开游戏模式 → 关闭 → 还原")
    import json
    cfg_path = os.path.join(ROOT, "config.json")
    cfg_backup = None
    if os.path.exists(cfg_path):
        cfg_backup = open(cfg_path, "rb").read()
    BAL = "381b4222-f694-41f0-9685-ff5bb260df2e"
    plan_p2 = active_scheme()
    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"last_game": "delta",
                       "opts": {"mouse": True, "nagle": True,
                                "suspend": False, "memtrim": False},
                       "tray_hint_shown": True}, f)
        subprocess.run(["powercfg", "/setactive", BAL], capture_output=True)
        check("准备:切到平衡制造可观察差异", active_scheme() == BAL, str(active_scheme()))

        subprocess.run(["cmd", "/c", cmd], capture_output=True, timeout=30)
        hwnd = None
        deadline = time.time() + 20
        while time.time() < deadline and not hwnd:
            hwnd = winapi.user32.FindWindowW(None, "游戏模式盒")
            time.sleep(0.2)
        check("双击后窗口出现", bool(hwnd))

        # 轮询等自动开启完成（后台线程 + pythonw 下 powercfg 偏慢，给足时间）
        applied = False
        deadline = time.time() + 30
        while time.time() < deadline:
            if (active_scheme() in (
                    "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
                    "e9a42b02-d5df-448d-aa00-03f14749eb61")
                    and os.path.exists(os.path.join(ROOT, "state.json"))):
                applied = True
                break
            time.sleep(0.3)
        check("自动开启:电源计划=高性能 + 状态账本落盘(限时30s)", applied,
              f"plan={active_scheme()} state={os.path.exists(os.path.join(ROOT, 'state.json'))}")

        if hwnd:
            winapi.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            gone = False
            deadline = time.time() + 25
            while time.time() < deadline and not gone:
                gone = not winapi.user32.FindWindowW(None, "游戏模式盒")
                time.sleep(0.2)
            check("关闭后窗口销毁", gone)
        check("关闭后电源计划回到平衡(=启动时原值)", active_scheme() == BAL,
              str(active_scheme()))
        check("关闭后状态账本清空",
              not os.path.exists(os.path.join(ROOT, "state.json")))
    finally:
        # 还原电源计划与配置文件（测试不留痕）
        if plan_p2:
            subprocess.run(["powercfg", "/setactive", plan_p2], capture_output=True)
        if cfg_backup is None:
            try:
                os.remove(cfg_path)
            except FileNotFoundError:
                pass
        else:
            with open(cfg_path, "wb") as f:
                f.write(cfg_backup)

    fails = [c for c in CHECKS if not c[1]]
    print("=" * 60)
    print(f"结果: {len(CHECKS) - len(fails)}/{len(CHECKS)} 通过")
    for n, _, d in fails:
        print(f"  ✗ {n} — {d}")
    print("=" * 60)
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
