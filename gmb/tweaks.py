"""优化项（Tweak）实现：每一项都「先记原值 → 再修改 → 回读验证」，退出全部还原。

要点：
- 原值一律落到 state.json（原子写）。程序被强杀没还原，下次启动 heal_leftovers() 自愈。
- 还原时按「原值精确回写」，不是像参考项目那样直接删值（删值会丢掉用户原有设置）。
- 需要管理员的项（Nagle）在非管理员下必须如实报失败，不许静默假成功。
- 所有对外读取都走 winreg/powercfg 直读，测试可用自己的独立读取器交叉验证。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass

import winreg

from . import winapi

# ---------------------------------------------------------------- 路径（可用环境变量重定向，测试隔离用）

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.environ.get("GMB_STATE") or os.path.join(BASE_DIR, "state.json")


@dataclass
class Result:
    ok: bool
    detail: str

    def __bool__(self) -> bool:  # pragma: no cover
        return self.ok


# ---------------------------------------------------------------- 状态存储（先记账再动手）


class StateStore:
    def __init__(self, path: str):
        self.path = path
        self.data: dict = {"applied": {}}
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict) and isinstance(loaded.get("applied"), dict):
                self.data = loaded
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self.data = {"applied": {}}

    def save(self) -> None:
        if not self.data["applied"]:
            # 账本空了就把文件删掉，不在项目目录留空壳残留
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def record(self, tweak_id: str, payload) -> None:
        self.data["applied"][tweak_id] = payload
        self.save()

    def get(self, tweak_id: str):
        return self.data["applied"].get(tweak_id)

    def clear(self, tweak_id: str) -> None:
        if tweak_id in self.data["applied"]:
            del self.data["applied"][tweak_id]
            self.save()

    def clear_all(self) -> None:
        if self.data["applied"]:
            self.data["applied"] = {}
            self.save()

    def applied_ids(self) -> list[str]:
        return list(self.data["applied"].keys())


STORE = StateStore(STATE_PATH)


# ---------------------------------------------------------------- 注册表工具

HIVES = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}
_R_ACCESS = winreg.KEY_READ | winreg.KEY_WOW64_64KEY
_W_ACCESS = winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY


def _jsonable(value):
    if isinstance(value, bytes):
        import base64
        return {"__b64__": base64.b64encode(value).decode("ascii")}
    return value


def _from_jsonable(value):
    if isinstance(value, dict) and "__b64__" in value:
        import base64
        return base64.b64decode(value["__b64__"])
    return value


def reg_read(hive: str, path: str, name: str) -> dict:
    """{existed, type, value}；读不到（键/值不存在）→ existed=False。"""
    try:
        with winreg.OpenKey(HIVES[hive], path, 0, _R_ACCESS) as k:
            value, typ = winreg.QueryValueEx(k, name)
            return {"existed": True, "type": int(typ), "value": _jsonable(value)}
    except FileNotFoundError:
        return {"existed": False, "type": None, "value": None}


def reg_write(hive: str, path: str, name: str, typ: int, value) -> dict:
    """写入并返回附加信息（是否由我们新建了键）。写失败抛 OSError。"""
    key_created = False
    try:
        winreg.OpenKey(HIVES[hive], path, 0, _W_ACCESS).Close()
    except FileNotFoundError:
        winreg.CreateKeyEx(HIVES[hive], path, 0, _W_ACCESS).Close()
        key_created = True
    with winreg.OpenKey(HIVES[hive], path, 0, _W_ACCESS) as k:
        winreg.SetValueEx(k, name, 0, typ, value)
    return {"key_created": key_created}


def reg_delete_value(hive: str, path: str, name: str) -> None:
    try:
        with winreg.OpenKey(HIVES[hive], path, 0, _W_ACCESS) as k:
            winreg.DeleteValue(k, name)
    except FileNotFoundError:
        pass


def reg_restore(rec: dict) -> Result:
    """把 reg_read 的原值精确写回（原无此值 → 删掉；我们建的空键 → 删键）。"""
    hive, path, name = rec["hive"], rec["path"], rec["name"]
    try:
        if rec.get("existed"):
            reg_write(hive, path, name, rec["type"], _from_jsonable(rec["value"]))
        else:
            reg_delete_value(hive, path, name)
            if rec.get("key_created"):
                try:
                    winreg.DeleteKey(HIVES[hive], path)
                except OSError:
                    pass  # 键里还有别的值/子键，保留
        # 回读核对
        now = reg_read(hive, path, name)
        if bool(now["existed"]) != bool(rec.get("existed")):
            return Result(False, "还原后回读不一致")
        if rec.get("existed") and now["value"] != rec["value"]:
            return Result(False, "还原后数值不一致")
        return Result(True, "已还原")
    except OSError as e:
        return Result(False, f"还原失败: {e}")


def _rec(hive: str, path: str, name: str, orig: dict, key_created: bool = False) -> dict:
    return {"hive": hive, "path": path, "name": name,
            "existed": orig["existed"], "type": orig["type"],
            "value": orig["value"], "key_created": key_created}


# ---------------------------------------------------------------- powercfg 工具

GUID_RE = r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
GUID_ULTIMATE = "e9a42b02-d5df-448d-aa00-03f14749eb61"   # 卓越性能
GUID_HIGH = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"       # 高性能
GUID_BALANCED = "381b4222-f694-41f0-9685-ff5bb260df2e"   # 平衡


def _dbg(msg: str) -> None:
    import datetime
    try:
        with open(os.path.join(BASE_DIR, "run.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} [tweaks] {msg}\n")
    except OSError:
        pass


def _powercfg(args: list[str]) -> tuple[bool, str]:
    import locale
    import time as _time
    t0 = _time.monotonic()
    try:
        p = subprocess.run(
            ["powercfg", *args],
            capture_output=True, timeout=20,
            # pythonw（双击路径）没有合法 stdin/句柄，子进程继承坏句柄会把
            # 每次调用拖慢到 1 秒以上（实测），必须显式 DEVNULL
            stdin=subprocess.DEVNULL,
            # Windows 控制台输出按系统 ANSI 代码页（本机 GBK）编码，
            # 用系统首选编码解码才能拿到可读的中文报错；GUID 是 ASCII 不受影响
            encoding=locale.getpreferredencoding(False) or "utf-8",
            errors="replace",
        )
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        dur = _time.monotonic() - t0
        if dur > 0.5:
            _dbg(f"powercfg {' '.join(args)} took {dur:.1f}s rc={p.returncode}")
        return p.returncode == 0, out
    except Exception as e:  # pragma: no cover
        _dbg(f"powercfg {' '.join(args)} exception: {e}")
        return False, str(e)


def active_scheme() -> str | None:
    ok, out = _powercfg(["/getactivescheme"])
    if not ok:
        return None
    m = re.search(GUID_RE, out, re.I)
    return m.group(1).lower() if m else None


def _registry_schemes() -> set[str]:
    """从注册表读全部电源方案 GUID —— 权威来源。

    实测陷阱：`powercfg -list` 会隐藏"未激活的隐藏方案"（本机的高性能计划
    不激活时就不出现在列表里，害得按列表判断直接误报"系统没有高性能计划"）。
    注册表 PowerSchemes 子键不受这个显示逻辑影响。
    """
    out: set[str] = set()
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes",
            0, _R_ACCESS,
        ) as k:
            i = 0
            while True:
                try:
                    out.add(winreg.EnumKey(k, i).lower())
                    i += 1
                except OSError:
                    break
    except OSError:
        pass
    return out


def list_schemes() -> set[str]:
    reg = _registry_schemes()
    if reg:
        return reg
    # 兜底：powercfg -list（注意不是 /list，那会报"参数无效"）
    ok, out = _powercfg(["-list"])
    if not ok:
        return set()
    return {m.lower() for m in re.findall(GUID_RE, out, re.I)}


# ---------------------------------------------------------------- Tweak 基类


class Tweak:
    id = ""
    name = ""
    desc = ""
    on_enable = True      # 开启游戏模式时是否参与
    default_opt = True    # 可选开关的默认值
    requires_admin = False

    def apply(self) -> Result:
        raise NotImplementedError

    def revert(self) -> Result:
        raise NotImplementedError

    def has_record(self) -> bool:
        return STORE.get(self.id) is not None


# ---------------------------------------------------------------- 各优化项


class PowerTweak(Tweak):
    id = "power"
    name = "高性能电源计划"
    desc = "游戏期间切到卓越性能/高性能，退出切回原计划"

    def apply(self) -> Result:
        cur = active_scheme()
        if cur is None:
            return Result(False, "读取当前电源计划失败")
        schemes = list_schemes()
        candidates: list[str] = []
        if GUID_ULTIMATE in schemes:
            # 卓越性能只在 powercfg -list 里可见时才尝试：本机实测它"注册表里有
            # 但 setactive 拒绝（rc=1 不受支持）"，盲目试每次白付 ~1 秒
            visible = {m.lower() for m in re.findall(
                GUID_RE, (_powercfg(["-list"])[1] or ""), re.I)}
            if GUID_ULTIMATE in visible:
                candidates.append(GUID_ULTIMATE)
        if GUID_HIGH in schemes:
            candidates.append(GUID_HIGH)  # 隐藏的高性能也直接可 setactive（实测）
        if not candidates:
            return Result(False, "系统未提供高性能/卓越性能电源计划")
        STORE.record(self.id, {"original": cur})
        last_out = ""
        for cand in candidates:
            ok, out = _powercfg(["/setactive", cand])
            if not ok:
                last_out = out or "setactive 失败"
                continue  # 失败就不必回读，省一次子进程（pythonw 下很贵）
            if active_scheme() == cand:
                label = "卓越性能" if cand == GUID_ULTIMATE else "高性能"
                detail = f"已切换：{label}"
                if cur == cand:
                    detail += "（原本就是）"
                if cand != GUID_ULTIMATE and GUID_ULTIMATE in schemes:
                    detail += "（本机卓越性能方案不可见，已用高性能）"
                return Result(True, detail)
            last_out = "回读不一致"
        STORE.clear(self.id)
        return Result(False, f"切换失败: {last_out[:80]}")

    def revert(self) -> Result:
        payload = STORE.get(self.id)
        if not payload:
            return Result(True, "无记录")
        original = payload.get("original")
        if not original:
            STORE.clear(self.id)
            return Result(True, "原计划未知，跳过")
        ok, out = _powercfg(["/setactive", original])
        verify = active_scheme()
        STORE.clear(self.id)
        if ok and verify == original:
            return Result(True, "已切回原电源计划")
        return Result(False, f"切回失败: {out[:80]}")


class GameModeTweak(Tweak):
    id = "gamemode"
    name = "Windows 游戏模式"
    desc = "AllowAutoGameMode=1，抑制后台活动（设置>游戏>游戏模式）"
    PATH = r"Software\Microsoft\GameBar"
    NAME = "AllowAutoGameMode"

    def apply(self) -> Result:
        orig = reg_read("HKCU", self.PATH, self.NAME)
        info = reg_write("HKCU", self.PATH, self.NAME, winreg.REG_DWORD, 1)
        STORE.record(self.id, _rec("HKCU", self.PATH, self.NAME, orig, info["key_created"]))
        now = reg_read("HKCU", self.PATH, self.NAME)
        if now["existed"] and now["value"] == 1:
            return Result(True, "已开启")
        return Result(False, "写入后回读不等于 1")

    def revert(self) -> Result:
        payload = STORE.get(self.id)
        if not payload:
            return Result(True, "无记录")
        res = reg_restore(payload)
        STORE.clear(self.id)
        return res


class GameDVRTweak(Tweak):
    id = "gamedvr"
    name = "关闭后台录制 (GameDVR)"
    desc = "停用 Xbox Game Bar 后台捕获，减少录屏开销"
    TARGETS = [
        (r"Software\Microsoft\Windows\CurrentVersion\GameDVR", "AppCaptureEnabled"),
        (r"System\GameConfigStore", "GameDVR_Enabled"),
    ]

    def apply(self) -> Result:
        items = []
        for path, name in self.TARGETS:
            orig = reg_read("HKCU", path, name)
            info = reg_write("HKCU", path, name, winreg.REG_DWORD, 0)
            items.append(_rec("HKCU", path, name, orig, info["key_created"]))
        STORE.record(self.id, {"items": items})
        bad = []
        for i in items:
            r = reg_read("HKCU", i["path"], i["name"])
            if not (r["existed"] and r["value"] == 0):
                bad.append(i["name"])
        if bad:
            return Result(False, f"写入后回读不为 0: {', '.join(bad)}")
        return Result(True, f"已关闭 {len(items)} 处后台录制开关")

    def revert(self) -> Result:
        payload = STORE.get(self.id)
        if not payload:
            return Result(True, "无记录")
        fails = []
        for i in payload["items"]:
            r = reg_restore(i)
            if not r.ok:
                fails.append(f"{i['name']}: {r.detail}")
        STORE.clear(self.id)
        return Result(not fails, "已还原" if not fails else "; ".join(fails))


class MouseTweak(Tweak):
    id = "mouse"
    name = "关闭鼠标加速"
    desc = "增强指针精度=关，抬枪更跟手（退出还原）"
    PATH = r"Control Panel\Mouse"
    NAMES = ("MouseSpeed", "MouseThreshold1", "MouseThreshold2")

    def apply(self) -> Result:
        items = []
        for name in self.NAMES:
            orig = reg_read("HKCU", self.PATH, name)
            info = reg_write("HKCU", self.PATH, name, winreg.REG_SZ, "0")
            items.append(_rec("HKCU", self.PATH, name, orig, info["key_created"]))
        STORE.record(self.id, {"items": items})
        now = all(
            (lambda r: r["existed"] and r["value"] == "0")(reg_read("HKCU", self.PATH, n))
            for n in self.NAMES
        )
        return Result(now, "已关闭鼠标加速" if now else "写入后回读不一致")

    def revert(self) -> Result:
        payload = STORE.get(self.id)
        if not payload:
            return Result(True, "无记录")
        ok = all(reg_restore(i).ok for i in payload["items"])
        STORE.clear(self.id)
        return Result(ok, "已还原鼠标设置" if ok else "部分值还原失败")


class NagleTweak(Tweak):
    id = "nagle"
    name = "关闭 Nagle 算法（网络加速）"
    desc = "降低 TCP 小包延迟；需要管理员权限"
    requires_admin = True
    PATH = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"
    VALUES = (("TcpAckFrequency", 1), ("TCPNoDelay", 1), ("TcpDelAckTicks", 0))

    @staticmethod
    def _subkeys() -> list[str]:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, NagleTweak.PATH, 0, _R_ACCESS) as k:
            out, i = [], 0
            while True:
                try:
                    out.append(winreg.EnumKey(k, i))
                    i += 1
                except OSError:
                    return out

    def apply(self) -> Result:
        if not winapi.is_admin():
            return Result(False, "需要管理员权限（可选优化，已跳过）")
        try:
            subs = self._subkeys()
        except OSError as e:
            return Result(False, f"读取网卡接口失败: {e}")
        items = []
        try:
            for sub in subs:
                path = f"{self.PATH}\\{sub}"
                for name, val in self.VALUES:
                    orig = reg_read("HKLM", path, name)
                    info = reg_write("HKLM", path, name, winreg.REG_DWORD, val)
                    items.append(_rec("HKLM", path, name, orig, info["key_created"]))
        except OSError as e:
            self._restore_items(items)
            return Result(False, f"写入失败(权限): {e}")
        STORE.record(self.id, {"items": items})
        # 回读：必须等于"目标值"，不是"原值"
        expect = [(f"{self.PATH}\\{sub}", name, val) for sub in subs for name, val in self.VALUES]
        bad = []
        for path, name, val in expect:
            r = reg_read("HKLM", path, name)
            if not (r["existed"] and r["value"] == val):
                bad.append(name)
        if bad:
            self._revert()
            return Result(False, f"回读不一致已回滚: {', '.join(bad)}")
        return Result(True, f"已禁用 Nagle：{len(subs)} 个网卡接口")

    @staticmethod
    def _restore_items(items: list[dict]) -> None:
        for i in reversed(items):
            reg_restore(i)

    def _revert(self) -> Result:
        payload = STORE.get(self.id)
        if not payload:
            return Result(True, "无记录")
        ok = True
        for i in reversed(payload["items"]):
            if not reg_restore(i).ok:
                ok = False
        STORE.clear(self.id)
        return Result(ok, "已还原" if ok else "部分值还原失败")

    def revert(self) -> Result:
        return self._revert()


class FullscreenTweak(Tweak):
    """运行期项：检测到游戏进程后，对其 exe 关闭全屏优化。"""
    id = "fullscreen"
    name = "关闭全屏优化"
    desc = "对游戏 exe 写入兼容性标志（退出还原）"
    PATH = r"Software\Microsoft\Windows\NT\CurrentVersion\AppCompatFlags\Layers"
    FLAG = "DISABLEDXMAXIMIZEDWINDOWEDMODE"

    def apply(self) -> Result:  # 由 apply_for_exe 驱动
        return Result(False, "由游戏检测驱动")

    def apply_for_exe(self, exe_path: str) -> Result:
        payload = STORE.get(self.id) or {"items": {}}
        items = payload["items"]
        key = exe_path.lower()
        if key in items:
            return Result(True, "已设置（幂等）")
        orig = reg_read("HKCU", self.PATH, exe_path)
        old = orig["value"] if (orig["existed"] and isinstance(orig["value"], str)) else None
        if old and self.FLAG in old.split():
            new = old
        elif old:
            new = old + " " + self.FLAG
        else:
            new = self.FLAG
        info = reg_write("HKCU", self.PATH, exe_path, winreg.REG_SZ, new)
        rec = _rec("HKCU", self.PATH, exe_path, orig, info["key_created"])
        rec["data_after"] = new
        items[key] = rec
        STORE.record(self.id, {"items": items})
        now = reg_read("HKCU", self.PATH, exe_path)
        if now["existed"] and isinstance(now["value"], str) and self.FLAG in now["value"].split():
            return Result(True, "已关闭该 exe 的全屏优化")
        return Result(False, "写入后回读不含目标标志")

    def revert(self) -> Result:
        payload = STORE.get(self.id)
        if not payload:
            return Result(True, "无记录")
        ok = True
        for key in list(payload["items"].keys()):
            if not reg_restore(payload["items"][key]).ok:
                ok = False
        STORE.clear(self.id)
        return Result(ok, "已还原全屏优化标志" if ok else "部分标志还原失败")


class PriorityTweak(Tweak):
    """运行期项：检测到游戏进程后提权到 High。"""
    id = "priority"
    name = "游戏进程优先级"
    desc = "游戏运行时提到「高」，退出还原原优先级"
    TARGET = "high"

    def apply(self) -> Result:
        return Result(False, "由游戏检测驱动")

    def apply_for_pid(self, pid: int, exe_name: str) -> Result:
        payload = STORE.get(self.id) or {"items": []}
        items = payload["items"]
        if any(i["pid"] == pid for i in items):
            return Result(True, "已提权（幂等）")
        original = winapi.get_priority(pid)
        if original is None:
            return Result(False, "读取游戏进程优先级失败")
        items.append({"pid": pid, "exe": exe_name, "original": original})
        STORE.record(self.id, {"items": items})
        ok, actual = winapi.set_priority(pid, self.TARGET)
        if ok:
            return Result(True, f"游戏进程 → 高优先级 (PID {pid})")
        return Result(False, f"设置失败，实际={actual} (PID {pid})")

    def revert(self) -> Result:
        payload = STORE.get(self.id)
        if not payload:
            return Result(True, "无记录")
        alive = winapi.list_processes()
        restored, gone = 0, 0
        for i in payload["items"]:
            if alive.get(i["pid"], "").lower() != i["exe"].lower():
                gone += 1
                continue  # 进程已退出，无可还原
            if i.get("original"):
                winapi.set_priority(i["pid"], i["original"])
                restored += 1
        STORE.clear(self.id)
        return Result(True, f"已还原 {restored} 个进程优先级" + (f"，{gone} 个已退出" if gone else ""))


class SuspendTweak(Tweak):
    id = "suspend"
    name = "挂起后台程序"
    desc = "暂停浏览器/游戏启动器（不是杀掉），退出全部恢复"
    on_enable = True
    default_opt = False
    TARGETS = {
        "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "brave.exe",
        "steam.exe", "epicgameslauncher.exe", "goggalaxy.exe", "origin.exe",
        "eadesktop.exe", "upc.exe", "ubisoftconnect.exe", "battle.net.exe",
    }

    def apply(self) -> Result:
        me = os.getpid()
        items = []
        for pid, exe in winapi.list_processes().items():
            low = exe.lower()
            if pid == me or low not in self.TARGETS:
                continue
            if winapi.suspend_process(pid):
                items.append({"pid": pid, "exe": exe})
        if items:
            STORE.record(self.id, {"items": items})
        return Result(True, f"已挂起 {len(items)} 个后台进程" if items
                      else "没有可挂起的后台进程")

    def revert(self) -> Result:
        payload = STORE.get(self.id)
        if not payload:
            return Result(True, "无记录")
        alive = winapi.list_processes()
        resumed, gone = 0, 0
        for i in payload["items"]:
            if alive.get(i["pid"], "").lower() != i["exe"].lower():
                gone += 1
                continue  # 进程已换代，绝不误恢复别人
            if winapi.resume_process(i["pid"]):
                resumed += 1
        STORE.clear(self.id)
        return Result(True, f"已恢复 {resumed} 个后台进程" + (f"，{gone} 个已退出" if gone else ""))


class MemTrimTweak(Tweak):
    """可选项：清空后台进程工作集。无需还原（工作集会自动回填），退出只停定时器。"""
    id = "memtrim"
    name = "清理后台内存"
    desc = "定时清空后台进程工作集（可选，默认关）"
    default_opt = False
    EXCLUDE = {
        "explorer.exe", "dwm.exe", "taskhostw.exe", "runtimebroker.exe",
        "searchhost.exe", "searchindexer.exe", "shellexperiencehost.exe",
        "applicationframehost.exe", "systemsettings.exe", "sihost.exe",
        "taskmgr.exe", "securityhealthservice.exe", "msmpeng.exe",
        "gamebar.exe", "gamingservices.exe",
    }
    GAME_EXES = {"deltaforce.exe", "uagame.exe", "abinfinite.exe",
                 "arenabreakoutinfinite.exe", "valorant-win64-shipping.exe"}

    def apply(self) -> Result:
        return Result(True, "已启用定时清理（30 秒/次）")

    def revert(self) -> Result:
        return Result(True, "已停止清理")

    def run_once(self) -> Result:
        me = os.getpid()
        done = fail = 0
        for pid, exe in winapi.list_processes().items():
            low = exe.lower()
            if pid == me or low in self.EXCLUDE or low in self.GAME_EXES:
                continue
            if winapi.empty_working_set(pid):
                done += 1
            else:
                fail += 1
        return Result(True, f"已清理 {done} 个进程" + (f"（{fail} 个无权限跳过）" if fail else ""))


# ---------------------------------------------------------------- 清单与编排

power = PowerTweak()
gamemode = GameModeTweak()
gamedvr = GameDVRTweak()
mouse = MouseTweak()
nagle = NagleTweak()
fullscreen = FullscreenTweak()
priority = PriorityTweak()
suspend = SuspendTweak()
memtrim = MemTrimTweak()

ALWAYS = [power, gamemode, gamedvr]                 # 必开
TOGGLEABLE = [mouse, nagle, suspend, memtrim]       # 可选开关
RUNTIME = [priority, fullscreen]                    # 由游戏检测驱动
ALL = ALWAYS + TOGGLEABLE + RUNTIME


def heal_leftovers() -> list[str]:
    """上次异常退出/强杀留下的改动 → 全部还原。返回人类可读日志行（无残留返回空表）。"""
    STORE.load()
    ids = set(STORE.applied_ids())
    if not ids:
        return []
    lines: list[str] = []
    # 还原顺序：运行期 → 可选 → 必开（倒序）
    for t in list(RUNTIME) + list(reversed(TOGGLEABLE)) + list(reversed(ALWAYS)):
        if t.id in ids:
            res = t.revert()
            lines.append(f"{t.name}: {'✓' if res.ok else '✗'} {res.detail}")
    STORE.load()
    if STORE.applied_ids():  # 仍有残留 = 还原没干净，如实上报
        lines.append("⚠ 仍有残留: " + ", ".join(STORE.applied_ids()))
    else:
        lines.append("上次残留已全部还原")
    return lines
