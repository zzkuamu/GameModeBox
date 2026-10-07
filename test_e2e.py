# -*- coding: utf-8 -*-
"""端到端自检：真实子进程启动 → 外部观测 → 模拟标题栏 X (WM_CLOSE) → 验证还原。

这是「双击打开 = 游戏模式开、关闭 = 全还原」需求的最终证据链：
- 配置里预置 last_game=delta，子进程必须自动进入游戏模式（独立回读电源/注册表）
- 外部 PostMessage(WM_CLOSE) = 用户点 X
- 子进程退出码 0、stderr 全空（正常关闭，不是崩溃）
- 关闭后系统状态回到启动前、状态账本清空

运行：python main.pyw --e2e
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from gmb import tweaks, winapi

CHECKS: list[tuple[str, bool, str]] = []
GUID_HIGH = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
GUID_ULTIMATE = "e9a42b02-d5df-448d-aa00-03f14749eb61"
GUID_BALANCED = "381b4222-f694-41f0-9685-ff5bb260df2e"
WM_CLOSE = 0x0010


def check(name: str, cond: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""),
          flush=True)
    return bool(cond)


def iactive() -> str | None:
    p = subprocess.run(["powercfg", "/getactivescheme"], capture_output=True,
                       timeout=20, encoding="utf-8", errors="replace")
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                  (p.stdout or "").lower())
    return m.group(1) if m else None


def ireg(path: str, name: str):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            v, _t = winreg.QueryValueEx(k, name)
            return (True, v)
    except FileNotFoundError:
        return (False, None)


def run() -> int:
    print("=" * 60)
    print("GameModeBox 端到端自检 (e2e)")
    print("=" * 60)

    if winapi.user32.FindWindowW(None, "游戏模式盒"):
        print("  [SKIP] 已有游戏模式盒在运行，先关掉它再跑 e2e")
        return 1

    TMP = tempfile.mkdtemp(prefix="gmb_e2e_")
    cfg_path = os.path.join(TMP, "config.json")
    state_path = os.path.join(TMP, "state.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"last_game": "delta",
                   "opts": {"mouse": True, "nagle": True,
                            "suspend": False, "memtrim": False},
                   "tray_hint_shown": True}, f)

    p0 = iactive()
    gm0 = ireg(r"Software\Microsoft\GameBar", "AllowAutoGameMode")
    # 制造可观察差异：先切到平衡
    subprocess.run(["powercfg", "/setactive", GUID_BALANCED], capture_output=True)
    prep_ok = iactive() == GUID_BALANCED

    env = dict(os.environ)
    env["GMB_STATE"] = state_path
    env["GMB_CONFIG"] = cfg_path
    env["PYTHONIOENCODING"] = "utf-8"

    proc = None
    try:
        base = tweaks.BASE_DIR
        proc = subprocess.Popen(
            [sys.executable, os.path.join(base, "main.pyw"), "--smoke"],
            cwd=base, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace")

        lines: list[str] = []

        def _reader():
            for line in proc.stdout:
                lines.append(line)

        threading.Thread(target=_reader, daemon=True).start()

        # 1) 窗口出现
        hwnd = None
        deadline = time.time() + 20
        while time.time() < deadline and not hwnd:
            hwnd = winapi.user32.FindWindowW(None, "游戏模式盒")
            time.sleep(0.2)
        check("窗口已创建", bool(hwnd))

        # 2) 就绪标记
        ready = False
        deadline = time.time() + 15
        while time.time() < deadline and not ready:
            ready = any("SMOKE_READY" in l for l in lines)
            time.sleep(0.2)
        check("收到 SMOKE_READY(启动流程走完)", ready, str(lines[-3:]))

        # 3) 自动开启游戏模式（独立回读）——优化在后台线程跑，轮询等待落地
        applied: set = set()
        ok_auto = False
        deadline = time.time() + 25
        while time.time() < deadline:
            plan_ok = (not prep_ok) or iactive() in (GUID_HIGH, GUID_ULTIMATE)
            gm_ok = ireg(r"Software\Microsoft\GameBar",
                         "AllowAutoGameMode") == (True, 1)
            st_ok = False
            try:
                with open(state_path, encoding="utf-8") as f:
                    applied = set(json.load(f).get("applied", {}).keys())
                st_ok = {"power", "gamemode", "gamedvr"} <= applied
            except Exception:
                applied = set()
            if plan_ok and gm_ok and st_ok:
                ok_auto = True
                break
            time.sleep(0.3)
        if prep_ok:
            check("自动开启:独立回读电源计划=高性能/卓越(限时25s)",
                  iactive() in (GUID_HIGH, GUID_ULTIMATE), str(iactive()))
        check("自动开启:独立回读游戏模式注册表=1",
              ireg(r"Software\Microsoft\GameBar", "AllowAutoGameMode") == (True, 1))
        check("状态账本含 power/gamemode/gamedvr",
              {"power", "gamemode", "gamedvr"} <= applied, str(applied))
        check("自动开启整体在限时内完成", ok_auto)

        # 4) 外部 WM_CLOSE = 用户点标题栏 X
        winapi.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        try:
            rc = proc.wait(timeout=30)  # 还原在后台线程跑（powercfg 可到秒级）
        except subprocess.TimeoutExpired:
            rc = None
        check("子进程在 WM_CLOSE 后正常退出(exit 0)", rc == 0, f"rc={rc}")

        stderr = proc.stderr.read() if proc.stderr else ""
        check("stderr 全空(不是崩溃)", stderr.strip() == "", stderr[:300])

        check("独立回读:关闭后注册表已还原",
              ireg(r"Software\Microsoft\GameBar", "AllowAutoGameMode") == gm0,
              str(ireg(r"Software\Microsoft\GameBar", "AllowAutoGameMode")))
        if prep_ok:
            check("独立回读:关闭后电源计划回到平衡(=启动时原值)",
                  iactive() == GUID_BALANCED, str(iactive()))
        try:
            with open(state_path, encoding="utf-8") as f:
                applied2 = json.load(f).get("applied", {})
        except Exception:
            applied2 = {}
        check("关闭后状态账本已清空", applied2 == {}, str(applied2))
        check("窗口句柄已销毁",
              not winapi.user32.FindWindowW(None, "游戏模式盒"))

    finally:
        if proc and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        if p0:
            subprocess.run(["powercfg", "/setactive", p0], capture_output=True)
        shutil.rmtree(TMP, ignore_errors=True)

    fails = [c for c in CHECKS if not c[1]]
    print("\n" + "=" * 60)
    print(f"结果: {len(CHECKS) - len(fails)}/{len(CHECKS)} 通过")
    for name, _, detail in fails:
        print(f"  ✗ {name} — {detail}")
    print("=" * 60)
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(run())
