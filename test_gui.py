# -*- coding: utf-8 -*-
"""界面行为自检（进程内）：结构断言 + 真实点击按钮 + 独立回读系统状态 + 截图。

覆盖：
- 结构：卡片/按钮/托盘/状态行都存在
- 行为：点卡片=开启（独立回读电源计划/注册表确实变了）、点关闭按钮=还原、点收托盘=窗口隐藏
- 关闭路径：w.close()（= 标题栏 X 的同一代码路径）触发 closeEvent → 必须全部还原
- 崩溃保护：任何断言失败 finally 也无条件还原系统状态
- 绘制异常必须报红（excepthook 捕获 paintEvent 里被 Qt 吞掉的异常）

运行：python main.pyw --gui-test
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="gmb_guitest_")
os.environ["GMB_STATE"] = os.path.join(TMP, "state.json")
os.environ["GMB_CONFIG"] = os.path.join(TMP, "config.json")

import winreg  # noqa: E402

from PySide6.QtWidgets import QSystemTrayIcon  # noqa: E402

from gmb import tweaks, winapi  # noqa: E402
from gmb.app import MainWindow  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []
PYERR: list[str] = []

GUID_HIGH = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
GUID_ULTIMATE = "e9a42b02-d5df-448d-aa00-03f14749eb61"


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


def ireg(path: str, name: str) -> tuple[bool, object]:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            v, _t = winreg.QueryValueEx(k, name)
            return True, v
    except FileNotFoundError:
        return False, None


def applied_ids_from_disk() -> set[str]:
    """独立读状态账本文件（不经过 STORE）。"""
    try:
        import json
        with open(os.environ["GMB_STATE"], encoding="utf-8") as f:
            return set(json.load(f).get("applied", {}).keys())
    except Exception:
        return set()


def run() -> int:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QPushButton

    sys.excepthook = lambda t, v, tb: PYERR.append(f"{t.__name__}: {v}")

    print("=" * 60)
    print("GameModeBox 界面行为自检 (gui-test)")
    print("=" * 60)

    p0 = iactive()
    gm0 = ireg(r"Software\Microsoft\GameBar", "AllowAutoGameMode")
    mouse0 = ireg(r"Control Panel\Mouse", "MouseSpeed")

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)  # 真机同款设置

    import time as _time

    def wait_idle(timeout: float = 40) -> bool:
        """等后台开启/还原完成（优化在工作线程里跑，UI 只是调度）。"""
        end = _time.time() + timeout
        while _time.time() < end and w_ref[0] is not None and w_ref[0]._phase:
            app.processEvents()
            _time.sleep(0.03)
        app.processEvents()
        return w_ref[0] is not None and w_ref[0]._phase is None

    def wait_hidden(timeout: float = 40) -> bool:
        """等异步还原完成后窗口真正关闭。"""
        end = _time.time() + timeout
        while _time.time() < end and w_ref[0] is not None and w_ref[0].isVisible():
            app.processEvents()
            _time.sleep(0.03)
        app.processEvents()
        return w_ref[0] is not None and not w_ref[0].isVisible()

    w_ref: list = [None]

    w = None
    try:
        w = MainWindow()
        w_ref[0] = w
        w.show()
        app.processEvents()

        # ---------- 结构断言 ----------
        print("\n[A] 结构")
        for key in ("delta", "abi", "valorant"):
            card = w.findChild(QPushButton, f"btn_game_{key}")
            check(f"存在游戏卡片 btn_game_{key}", card is not None)
        check("存在关闭游戏模式按钮", w.findChild(QPushButton, "big") is not None
              or w.btn_toggle is not None)
        check("存在收进托盘按钮", w.btn_hide is not None)
        check("状态行齐全", set(w.row_labels) >=
              {"power", "gamemode", "gamedvr", "priority", "fullscreen"})
        tray_ok = QSystemTrayIcon.isSystemTrayAvailable()
        check("系统托盘可用", tray_ok)
        if tray_ok:
            check("托盘图标已创建且可见", w.tray is not None and w.tray.isVisible())
        check("初始状态:游戏模式未开启", w.mode_on is False)
        check("初始状态:关闭按钮禁用", w.btn_toggle.isEnabled() is False)

        docs = os.path.join(tweaks.BASE_DIR, "docs")
        os.makedirs(docs, exist_ok=True)
        shot_off = os.path.join(docs, "screenshot-idle.png")
        w.grab().save(shot_off)
        check("截图(未开启)已保存", os.path.exists(shot_off), shot_off)

        # 渲染断言（截图肉眼会被缓存骗，必须走像素）：
        # 卡片若回退成"显示 HTML 源码"，暗色文字会横贯整卡（x>400）；
        # 渲染后的短标题只到 ~140。这里把它钉死。
        def _card_dark_extent(path) -> int:
            from PySide6.QtGui import QImage
            img = QImage(path)
            xmax = -1
            for y in range(int(img.height() * 0.085), int(img.height() * 0.15)):
                for x in range(5, img.width() - 5):
                    c = img.pixelColor(x, y)
                    if c.red() < 90 and c.green() < 90 and c.blue() < 90:
                        xmax = max(xmax, x)
            return xmax

        ext = _card_dark_extent(shot_off)
        check("卡片文字是渲染结果(非HTML源码)", 0 < ext < 300, f"暗像素最右 x={ext}")
        check("按钮本体不再承载HTML文本(QLabel负责渲染)",
              all(card.text() == "" for card in w.cards.values()))

        # ---------- 行为：点卡片 = 开启 ----------
        print("\n[B] 点击游戏卡片 → 游戏模式开启（独立回读）")
        card = w.findChild(QPushButton, "btn_game_delta")
        card.click()
        app.processEvents()
        check("后台应用在限时内完成", wait_idle(40), f"phase={w._phase}")
        check("模式状态=开启", w.mode_on is True)
        check("独立回读:电源计划=高性能/卓越",
              iactive() in (GUID_HIGH, GUID_ULTIMATE), str(iactive()))
        check("独立回读:游戏模式注册表=1",
              ireg(r"Software\Microsoft\GameBar", "AllowAutoGameMode") == (True, 1))
        check("独立回读:GameDVR=0",
              ireg(r"Software\Microsoft\Windows\CurrentVersion\GameDVR",
                   "AppCaptureEnabled") == (True, 0))
        check("独立回读:鼠标加速=0",
              ireg(r"Control Panel\Mouse", "MouseSpeed") == (True, "0"))
        ids = applied_ids_from_disk()
        check("状态账本落盘(power/gamemode/gamedvr)",
              {"power", "gamemode", "gamedvr"} <= ids, str(ids))
        check("状态 pill 显示运行中", w.pill.text() == "运行中", w.pill.text())
        check("关闭按钮已启用", w.btn_toggle.isEnabled() is True)
        shot_on = os.path.join(docs, "screenshot.png")
        w.grab().save(shot_on)
        check("截图(运行中)已保存", os.path.exists(shot_on), shot_on)
        ext_on = _card_dark_extent(shot_on)
        check("运行中卡片同样为渲染结果", 0 < ext_on < 300, f"x={ext_on}")
        # 状态胶囊必须是绿色底（运行中）
        from PySide6.QtGui import QImage
        img = QImage(shot_on)
        r = g = b = n = 0
        for y in range(8, 30, 2):
            for x in range(img.width() - 90, img.width() - 15, 2):
                c = img.pixelColor(x, y)
                r += c.red(); g += c.green(); b += c.blue(); n += 1
        avg = (r // n, g // n, b // n)
        check("状态胶囊为绿色(像素级)", avg[1] > avg[0] and avg[1] > avg[2],
              f"pill avg={avg}")

        # ---------- 行为：点收进托盘 ----------
        print("\n[C] 点击收进托盘")
        w.btn_hide.click()
        app.processEvents()
        check("窗口已隐藏到托盘", w.isVisible() is False)
        if tray_ok:
            check("托盘图标仍在", w.tray.isVisible())
        w._show_from_tray()  # 托盘「打开主界面」的同一槽
        app.processEvents()
        check("从托盘恢复显示", w.isVisible() is True)

        # ---------- 行为：点关闭游戏模式按钮 ----------
        print("\n[D] 点击关闭游戏模式 → 全部还原（独立回读）")
        w.btn_toggle.click()
        app.processEvents()
        check("后台还原在限时内完成", wait_idle(40), f"phase={w._phase}")
        check("模式状态=关闭", w.mode_on is False)
        check("独立回读:电源计划回到原值", iactive() == p0, f"{iactive()} vs {p0}")
        check("独立回读:游戏模式注册表还原",
              ireg(r"Software\Microsoft\GameBar", "AllowAutoGameMode") == gm0)
        check("独立回读:鼠标加速还原",
              ireg(r"Control Panel\Mouse", "MouseSpeed") == mouse0)
        check("状态账本已清空", applied_ids_from_disk() == set(),
              str(applied_ids_from_disk()))
        check("窗口仍开着(只是关了模式)", w.isVisible() is True)

        # ---------- 关闭路径：X（closeEvent）= 还原并退出 ----------
        print("\n[E] 再次开启 → 关闭窗口(X) → 必须还原")
        w.findChild(QPushButton, "btn_game_delta").click()
        app.processEvents()
        check("再次开启成功(后台完成)", wait_idle(40), f"phase={w._phase}")
        check("mode_on=True", w.mode_on is True)
        check("独立回读:注册表已被改动",
              ireg(r"Software\Microsoft\GameBar", "AllowAutoGameMode") == (True, 1))
        w.close()  # 与点标题栏 X 完全同一条代码路径（异步还原后才真正关闭）
        app.processEvents()
        check("X 后窗口在限时内关闭", wait_hidden(40))
        check("closeEvent 标记退出", w._quitting is True)
        check("关闭=模式已关闭", w.mode_on is False)
        check("独立回读:关闭后电源计划已还原", iactive() == p0, str(iactive()))
        check("独立回读:关闭后注册表已还原",
              ireg(r"Software\Microsoft\GameBar", "AllowAutoGameMode") == gm0)
        check("关闭后状态账本清空", applied_ids_from_disk() == set())

        # ---------- 绘制/信号异常 ----------
        print("\n[F] 运行期异常")
        check("无未捕获异常(含 paintEvent)", PYERR == [], "; ".join(PYERR))

    finally:
        # 无条件安全网
        try:
            for t in list(tweaks.RUNTIME) + list(reversed(tweaks.TOGGLEABLE)) + \
                    list(reversed(tweaks.ALWAYS)):
                t.revert()
            tweaks.STORE.clear_all()
        except Exception as e:  # pragma: no cover
            print(f"  [cleanup] {e}")
        if p0 and iactive() != p0:
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
