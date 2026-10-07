# -*- coding: utf-8 -*-
"""逻辑自检：真实改系统设置 → 独立回读验证 → 还原 → 独立回读验证。

三条纪律（写给自己）：
1. 断言必须用测试自己实现的读取器（winreg/powercfg 直读），不许调用被测模块的读取函数。
2. 必须有负向测试：注入「说谎的优化项」，检查逻辑必须报红；只报绿的检查器没用。
3. 无论断言成败，收尾无条件还原系统状态（电源计划、注册表、子进程）。

运行：python main.pyw --selftest   （退出码 0=全绿）
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

# ---- 隔离：必须在 import gmb.tweaks 之前设置，绝不碰项目里真实 state/config ----
TMP = tempfile.mkdtemp(prefix="gmb_selftest_")
os.environ["GMB_STATE"] = os.path.join(TMP, "state.json")
os.environ["GMB_CONFIG"] = os.path.join(TMP, "config.json")

import winreg  # noqa: E402

from gmb import tweaks, winapi  # noqa: E402
from gmb.detect import find_game  # noqa: E402
from gmb.games import GAMES  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []
CHILDREN: list[subprocess.Popen] = []
P0_PLAN: str | None = None

GUID_BALANCED = "381b4222-f694-41f0-9685-ff5bb260df2e"
GUID_HIGH = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
GUID_ULTIMATE = "e9a42b02-d5df-448d-aa00-03f14749eb61"


def check(name: str, cond: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(cond), detail))
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return bool(cond)


# ================================================== 测试自己的独立读取器

def ir(hive: str, path: str, name: str) -> tuple[bool, object]:
    """independent read：测试自己读注册表，不经过 tweaks.reg_read。"""
    key = winreg.HKEY_CURRENT_USER if hive == "HKCU" else winreg.HKEY_LOCAL_MACHINE
    try:
        with winreg.OpenKey(key, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            v, _t = winreg.QueryValueEx(k, name)
            return True, v
    except FileNotFoundError:
        return False, None


def iactive() -> str | None:
    """independent read：测试自己跑 powercfg。"""
    p = subprocess.run(["powercfg", "/getactivescheme"], capture_output=True,
                       timeout=20, encoding="utf-8", errors="replace")
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                  (p.stdout or "").lower())
    return m.group(1) if m else None


def setplan(guid: str) -> bool:
    p = subprocess.run(["powercfg", "/setactive", guid], capture_output=True, timeout=20)
    return p.returncode == 0 and iactive() == guid


def spawn(args: list[str]) -> subprocess.Popen:
    p = subprocess.Popen(args, cwd=TMP,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    CHILDREN.append(p)
    return p


def wait_for(fn, timeout: float, interval: float = 0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    return None


def wait_pid_name(pid: int) -> str | None:
    def _hit():
        name = winapi.list_processes().get(pid)
        return name if name else None
    return wait_for(_hit, 5)


# ================================================== 各项测试

def t0_isolation() -> None:
    print("\n[0] 隔离与环境")
    check("状态文件已隔离到临时目录", tweaks.STATE_PATH.startswith(TMP), tweaks.STATE_PATH)
    check("配置文件已隔离", os.environ["GMB_CONFIG"].startswith(TMP))
    real_state = os.path.join(tweaks.BASE_DIR, "state.json")
    check("项目目录无真实状态文件(首次运行)", not os.path.exists(real_state))


def t1_power() -> None:
    print("\n[1] 电源计划：切换 → 独立回读 → 还原 → 独立回读")
    p0 = iactive()
    check("能读到当前电源计划", p0 is not None, str(p0))
    prepared = setplan(GUID_BALANCED)
    check("准备动作:可切到平衡(制造可观察差异)", prepared and iactive() == GUID_BALANCED,
          f"current={iactive()}")
    try:
        res = tweaks.power.apply()
        check("power.apply 返回 ok", res.ok, res.detail)
        after = iactive()
        check("独立回读 = 高性能或卓越性能", after in (GUID_HIGH, GUID_ULTIMATE), str(after))
        r2 = tweaks.power.revert()
        check("power.revert ok", r2.ok, r2.detail)
        expect = GUID_BALANCED if prepared else p0
        check("独立回读 = 还原到切换前", iactive() == expect, f"{iactive()} vs {expect}")
        # 幂等负向：再 revert 一次必须安全
        r3 = tweaks.power.revert()
        check("重复 revert 安全(无记录)", r3.ok, r3.detail)
    finally:
        if p0 and iactive() != p0:
            setplan(p0)
        check("收尾:恢复测试前的电源计划", iactive() == p0, f"{iactive()} vs {p0}")


def _registry_roundtrip(name, t, hive, path, target_names, new_values) -> None:
    origs = [ir(hive, path, n) for n in target_names]
    res = t.apply()
    check(f"{name}.apply ok", res.ok, res.detail)
    now = [ir(hive, path, n) for n in target_names]
    check(f"{name} 独立回读=目标值",
          all(ex and v == nv for (ex, v), nv in zip(now, new_values)),
          str(now))
    r2 = t.revert()
    check(f"{name}.revert ok", r2.ok, r2.detail)
    now2 = [ir(hive, path, n) for n in target_names]
    check(f"{name} 独立回读=原值", now2 == origs, f"{now2} vs {origs}")


def _iw(hive: str, path: str, name: str, typ: int, value) -> None:
    """测试自己写注册表（准备动作/收尾恢复）。"""
    key = winreg.HKEY_CURRENT_USER if hive == "HKCU" else winreg.HKEY_LOCAL_MACHINE
    with winreg.CreateKeyEx(key, path, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, typ, value)


def _ir_restore(hive: str, path: str, name: str, orig: tuple) -> None:
    existed, val = orig
    if existed:
        _iw(hive, path, name, winreg.REG_DWORD if isinstance(val, int) else winreg.REG_SZ, val)
    else:
        key = winreg.HKEY_CURRENT_USER if hive == "HKCU" else winreg.HKEY_LOCAL_MACHINE
        try:
            with winreg.OpenKey(key, path, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, name)
        except FileNotFoundError:
            pass


def t2_registry_tweaks() -> None:
    print("\n[2] 注册表类：游戏模式 / GameDVR / 鼠标加速（准备→改→回读→还原→回读）")
    gm_path = r"Software\Microsoft\GameBar"
    dv_targets = tweaks.GameDVRTweak.TARGETS
    ms_path = r"Control Panel\Mouse"
    ms_names = ("MouseSpeed", "MouseThreshold1", "MouseThreshold2")

    # 先记录用户真实原值（本函数收尾无条件恢复）
    gm_orig0 = ir("HKCU", gm_path, "AllowAutoGameMode")
    dv_orig0 = [ir("HKCU", p, n) for p, n in dv_targets]
    ms_orig0 = [ir("HKCU", ms_path, n) for n in ms_names]
    try:
        # 准备：把当前值改成与目标不同的值，让"还原"方向也能被观察到
        _iw("HKCU", gm_path, "AllowAutoGameMode", winreg.REG_DWORD, 0)
        _registry_roundtrip("gamemode", tweaks.gamemode, "HKCU",
                            gm_path, ["AllowAutoGameMode"], [1])

        for p, n in dv_targets:
            _iw("HKCU", p, n, winreg.REG_DWORD, 1)
        g_before = [ir("HKCU", p, n) for p, n in dv_targets]
        res = tweaks.gamedvr.apply()
        check("gamedvr.apply ok", res.ok, res.detail)
        g_now = [ir("HKCU", p, n) for p, n in dv_targets]
        check("gamedvr 独立回读=0", all(ex and v == 0 for ex, v in g_now), str(g_now))
        r2 = tweaks.gamedvr.revert()
        check("gamedvr.revert ok", r2.ok, r2.detail)
        check("gamedvr 独立回读=还原到准备值(1)",
              [ir("HKCU", p, n) for p, n in dv_targets] == g_before,
              str([ir("HKCU", p, n) for p, n in dv_targets]))

        for n in ms_names:
            _iw("HKCU", ms_path, n, winreg.REG_SZ, "7")
        _registry_roundtrip("mouse", tweaks.mouse, "HKCU",
                            ms_path, list(ms_names), ["0", "0", "0"])
    finally:
        _ir_restore("HKCU", gm_path, "AllowAutoGameMode", gm_orig0)
        for (p, n), orig in zip(dv_targets, dv_orig0):
            _ir_restore("HKCU", p, n, orig)
        for n, orig in zip(ms_names, ms_orig0):
            _ir_restore("HKCU", ms_path, n, orig)
        check("收尾:三个注册表测试全部恢复用户原值",
              ir("HKCU", gm_path, "AllowAutoGameMode") == gm_orig0
              and [ir("HKCU", p, n) for p, n in dv_targets] == dv_orig0
              and [ir("HKCU", ms_path, n) for n in ms_names] == ms_orig0)


def t3_nagle() -> None:
    print("\n[3] Nagle（非管理员必须如实失败，不许假成功）")
    admin = winapi.is_admin()
    print(f"  (当前管理员={admin})")
    before = tweaks.STORE.get("nagle")
    res = tweaks.nagle.apply()
    if not admin:
        check("非管理员下 apply 报失败", not res.ok, res.detail)
        check("失败详情指明需要管理员", "管理员" in res.detail, res.detail)
        check("失败时不写状态账", tweaks.STORE.get("nagle") is None)
    else:
        check("管理员下 apply ok", res.ok, res.detail)
        r2 = tweaks.nagle.revert()
        check("nagle.revert ok", r2.ok, r2.detail)


def t4_fullscreen() -> None:
    print("\n[4] 全屏优化标志（假 exe 路径，含「已有旧值须保留」场景）")
    fake = os.path.join(TMP, "fake_game_a.exe")
    r1 = tweaks.fullscreen.apply_for_exe(fake)
    check("apply ok", r1.ok, r1.detail)
    ex, val = ir("HKCU", tweaks.FullscreenTweak.PATH, fake)
    check("独立回读含目标标志",
          ex and tweaks.FullscreenTweak.FLAG in str(val).split(), str(val))
    # 场景2：已有旧标志 → 必须合并而不是覆盖
    fake2 = os.path.join(TMP, "fake_game_b.exe")
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                            tweaks.FullscreenTweak.PATH, 0,
                            winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, fake2, 0, winreg.REG_SZ, "HIGHDPIAWARE")
    r2 = tweaks.fullscreen.apply_for_exe(fake2)
    ex2, val2 = ir("HKCU", tweaks.FullscreenTweak.PATH, fake2)
    check("旧标志被保留", r2.ok and "HIGHDPIAWARE" in str(val2).split(), str(val2))
    r3 = tweaks.fullscreen.revert()
    check("revert ok", r3.ok, r3.detail)
    check("fake_a 标志已删除", ir("HKCU", tweaks.FullscreenTweak.PATH, fake)[0] is False)
    ex3, val3 = ir("HKCU", tweaks.FullscreenTweak.PATH, fake2)
    check("fake2 精确还原为旧值", ex3 and val3 == "HIGHDPIAWARE", str(val3))


def t5_priority() -> None:
    print("\n[5] 进程优先级（真实子进程：提权 → 回读 → 还原 → 回读）")
    child = spawn([sys.executable, "-c", "import time; time.sleep(30)"])
    time.sleep(0.3)
    exe = wait_pid_name(child.pid)
    if not exe:
        check("子进程可见", False, "拿不到子进程")
        return
    orig = winapi.get_priority(child.pid)
    check("能读到子进程原优先级", orig is not None, str(orig))
    res = tweaks.priority.apply_for_pid(child.pid, exe.lower())
    check("apply ok", res.ok, res.detail)
    check("独立回读=high", winapi.get_priority(child.pid) == "high",
          str(winapi.get_priority(child.pid)))
    r2 = tweaks.priority.revert()
    check("revert ok", r2.ok, r2.detail)
    check("独立回读=原优先级", winapi.get_priority(child.pid) == orig,
          f"{winapi.get_priority(child.pid)} vs {orig}")
    child.terminate()


def t6_suspend() -> None:
    print("\n[6] 挂起/恢复：行为级验证（子进程真的停下来/真的继续）")
    m_start = os.path.join(TMP, "s_start.txt")
    m_done = os.path.join(TMP, "s_done.txt")
    for f in (m_start, m_done):
        if os.path.exists(f):
            os.remove(f)
    code = (f"import time; open({m_start!r},'w').write('x'); "
            f"time.sleep(6); open({m_done!r},'w').write('y')")
    child = spawn([sys.executable, "-c", code])
    check("子进程启动并写标记", wait_for(lambda: os.path.exists(m_start), 8) is not None)
    exe_name = wait_pid_name(child.pid) or "python.exe"

    check("挂起调用成功", winapi.suspend_process(child.pid))
    time.sleep(3)
    check("挂起期间不再推进(3秒后仍未完成)", not os.path.exists(m_done))

    # 负向：名字不匹配的账 → revert 绝不能误恢复
    tweaks.STORE.record("suspend", {"items": [
        {"pid": child.pid, "exe": "notepad.exe"}]})
    r = tweaks.suspend.revert()
    check("错名账:resumed=0", "恢复 0" in r.detail, r.detail)
    time.sleep(2)
    check("错名账:进程仍处于挂起(没被误恢复)", not os.path.exists(m_done))

    # 正向：正确名字的账 → revert 恢复，子进程继续跑完
    tweaks.STORE.record("suspend", {"items": [
        {"pid": child.pid, "exe": exe_name.lower()}]})
    r2 = tweaks.suspend.revert()
    check("对名账:revert ok", r2.ok, r2.detail)
    check("恢复后子进程真的跑完了(≤8s)", wait_for(lambda: os.path.exists(m_done), 8) is not None)
    child.terminate()


def t7_memtrim() -> None:
    print("\n[7] 内存清理 EmptyWorkingSet（吃 200MB 内存的子进程）")
    done_mark = os.path.join(TMP, "mem_filled.txt")
    if os.path.exists(done_mark):
        os.remove(done_mark)
    code = ("import time; x=bytearray(200*1024*1024); "
            "[x.__setitem__(i,1) for i in range(0,len(x),4096)]; "
            f"open({done_mark!r},'w').write('x'); time.sleep(30)")
    child = spawn([sys.executable, "-c", code])
    time.sleep(0.3)
    wait_pid_name(child.pid)

    # 必须等它摸完内存进入 sleep 再测，否则边清边写回，测量结果没有意义
    check("子进程已填充完内存",
          wait_for(lambda: os.path.exists(done_mark), 15) is not None)
    ws_before = winapi.working_set_bytes(child.pid)
    if not ws_before or ws_before < 100 * 1024 * 1024:
        check("子进程工作集已增长到 100MB+", False, str(ws_before))
        child.terminate()
        return
    ok = winapi.empty_working_set(child.pid)
    time.sleep(0.3)
    ws_after = winapi.working_set_bytes(child.pid)
    check("EmptyWorkingSet 调用成功(非管理员)", ok, str(ok))
    # 注意：清理干净 = 0 字节，0 是成功值，断言必须用 is not None 判断
    check("工作集显著下降",
          ws_after is not None and ws_after < ws_before * 0.6,
          f"{ws_before//1048576}MB → "
          f"{str(ws_after // 1048576) if ws_after is not None else 'None'}MB")
    r = tweaks.memtrim.run_once()
    check("run_once 正常返回", r.ok, r.detail)
    child.terminate()


def t8_detect() -> None:
    print("\n[8] 游戏检测双通道（负向：没跑=检不出；正向：制造真进程/真窗口）")
    for g in GAMES:
        det = find_game(g)
        check(f"负向: {g.name} 未运行时必须返回 None", det is None, str(det))

    # 正向 A：把 python.exe 连同依赖 DLL 一起复制改名成 DeltaForce.exe → 进程名通道
    fake_exe = os.path.join(TMP, "DeltaForce.exe")
    if not os.path.exists(fake_exe):
        shutil.copyfile(sys.executable, fake_exe)
        # python.exe 依赖同目录的 python3xx.dll 等，缺了会弹 DLL 缺失对话框
        src_dir = os.path.dirname(sys.executable)
        for f in os.listdir(src_dir):
            if f.lower().endswith(".dll"):
                dst = os.path.join(TMP, f)
                if not os.path.exists(dst):
                    shutil.copyfile(os.path.join(src_dir, f), dst)
    child_a = spawn([fake_exe, "-c", "import time; time.sleep(30)"])
    time.sleep(0.5)
    det = find_game(GAMES[0])
    check("正向A: 进程名通道识别 DeltaForce.exe",
          det is not None and det.exe_name == "deltaforce.exe",
          str(det))
    if det:
        check("正向A: 读到 exe 完整路径", bool(det.exe_path), str(det.exe_path))
    child_a.terminate()

    # 正向 B：真窗口标题「暗区突围无限测试」→ 窗口通道
    win_code = (
        "from PySide6.QtWidgets import QApplication, QLabel\n"
        "app = QApplication([])\n"
        "w = QLabel('x'); w.setWindowTitle('暗区突围无限-检测用测试窗口'); w.show()\n"
        "app.processEvents()\n"
        "import time; time.sleep(30)\n"
    )
    child_b = spawn([sys.executable, "-c", win_code])
    det2 = wait_for(lambda: find_game(GAMES[1]), 15, 0.5)
    check("正向B: 窗口标题通道识别（source=窗口）",
          det2 is not None and det2.source == "窗口", str(det2))
    child_b.terminate()
    # 等窗口消失后再确认回到 None（负向复位）
    time.sleep(1.5)
    check("负向复位: 窗口消失后回到 None",
          wait_for(lambda: find_game(GAMES[1]) is None, 10, 0.5) is not None)


def t9_negative_liar() -> None:
    print("\n[9] 负向测试：检查器必须能抓住「假成功」")

    class LyingTweak(tweaks.Tweak):
        """记账说改了、返回 ok，但实际一个字节都没写 —— 模拟被吞掉的权限错误。"""
        id = "lie"
        name = "注入的假优化项"

        def apply(self):
            tweaks.STORE.record(self.id, {"items": []})
            return tweaks.Result(True, "已生效(说谎)")

        def revert(self):
            tweaks.STORE.clear(self.id)
            return tweaks.Result(True, "已还原")

    path = r"Software\Microsoft\GameBar"
    vname = "AllowAutoGameMode_LIE_TEST"
    liar = LyingTweak()

    res = liar.apply()
    check("骗子自报 ok", res.ok, res.detail)
    ex, val = ir("HKCU", path, vname)
    # 检查逻辑 = 独立回读：它声称生效，但独立读取发现值不存在 → 必须判定为失败
    caught = not (res.ok and ex and val == 1)
    check("负向:独立回读抓到了假成功", caught, f"声称ok={res.ok}, 独立读到 existed={ex}")
    liar.revert()

    # 反向对照：真实 GameModeTweak 走同一套独立回读 → 必须通过
    r_ok = tweaks.gamemode.apply()
    ex2, val2 = ir("HKCU", path, "AllowAutoGameMode")
    green = r_ok.ok and ex2 and val2 == 1
    check("正向对照:真优化项能通过同样的检查", green, f"ok={r_ok.ok} read={val2}")
    tweaks.gamemode.revert()

    # 状态文件损坏 → 不许崩，按空处理
    with open(tweaks.STATE_PATH, "w", encoding="utf-8") as f:
        f.write('{"applied": { BROKEN')
    tweaks.STORE.load()
    check("损坏 state.json:load 不崩且按空处理", tweaks.STORE.applied_ids() == [])
    lines = tweaks.heal_leftovers()
    check("损坏 state.json:heal 不崩", isinstance(lines, list) and lines == [], str(lines))
    if os.path.exists(tweaks.STATE_PATH):
        os.remove(tweaks.STATE_PATH)


def t10_heal() -> None:
    print("\n[10] 崩溃自愈：模拟「改完没还原就被杀」")
    path = r"Software\Microsoft\GameBar"
    name = "AllowAutoGameMode"
    orig = ir("HKCU", path, name)
    res = tweaks.gamemode.apply()
    check("模拟崩溃前 apply 成功", res.ok and ir("HKCU", path, name) == (True, 1),
          str(ir("HKCU", path, name)))
    # 不调用 revert，直接 heal（= 崩溃后下次启动的路径）
    lines = tweaks.heal_leftovers()
    check("heal 还原了残留值", ir("HKCU", path, name) == orig,
          f"{ir('HKCU', path, name)} vs {orig}")
    check("heal 后状态账清空", tweaks.STORE.applied_ids() == [])
    check("heal 返回了日志", any("游戏模式" in l for l in lines), str(lines))


def t11_structure() -> None:
    print("\n[11] 清单结构")
    ids = [t.id for t in tweaks.ALWAYS + tweaks.TOGGLEABLE + tweaks.RUNTIME]
    check("清单无重复 id", len(ids) == len(set(ids)), str(ids))
    check("ALL 覆盖全部清单", set(ids) == {t.id for t in tweaks.ALL})
    check("GUI 依赖的行顺序齐全",
          set(tweaks.ALWAYS and ["power", "gamemode", "gamedvr"]) <= set(ids))
    check("运行期项与必开项不重叠",
          not ({t.id for t in tweaks.RUNTIME} & {t.id for t in tweaks.ALWAYS}))


# ================================================== 收尾安全网

def _cleanup() -> None:
    # 1) 所有优化项无条件 revert（大多已是 no-op）
    for t in list(tweaks.RUNTIME) + list(reversed(tweaks.TOGGLEABLE)) + \
            list(reversed(tweaks.ALWAYS)):
        try:
            t.revert()
        except Exception as e:  # pragma: no cover
            print(f"  [cleanup] {t.id} revert 异常: {e}")
    tweaks.STORE.clear_all()
    # 2) 电源计划回原
    if P0_PLAN:
        setplan(P0_PLAN)
    # 3) 子进程清理
    for p in CHILDREN:
        try:
            p.terminate()
        except Exception:
            pass
    for p in CHILDREN:
        try:
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    # 4) 临时目录
    shutil.rmtree(TMP, ignore_errors=True)


def run() -> int:
    global P0_PLAN
    print("=" * 60)
    print("GameModeBox 逻辑自检 (selftest)")
    print(f"python={sys.executable} admin={winapi.is_admin()}")
    print("=" * 60)
    P0_PLAN = iactive()
    try:
        t0_isolation()
        t1_power()
        t2_registry_tweaks()
        t3_nagle()
        t4_fullscreen()
        t5_priority()
        t6_suspend()
        t7_memtrim()
        t8_detect()
        t9_negative_liar()
        t10_heal()
        t11_structure()
    finally:
        _cleanup()

    fails = [c for c in CHECKS if not c[1]]
    print("\n" + "=" * 60)
    print(f"结果: {len(CHECKS) - len(fails)}/{len(CHECKS)} 通过")
    if fails:
        print("失败项:")
        for name, _, detail in fails:
            print(f"  ✗ {name} — {detail}")
    # 独立复核：系统状态真的还原干净了吗（不信测试自己的账本）
    print("\n[收尾独立复核]")
    ok_plan = iactive() == P0_PLAN
    print(f"  [{'PASS' if ok_plan else 'FAIL'}] 电源计划已回到测试前: {iactive()}")
    ok_state = not os.path.exists(os.path.join(tweaks.BASE_DIR, "state.json"))
    print(f"  [{'PASS' if ok_state else 'FAIL'}] 项目目录没有残留 state.json")
    final_fails = len(fails) + (0 if ok_plan else 1) + (0 if ok_state else 1)
    print("=" * 60)
    return 0 if final_fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run())
