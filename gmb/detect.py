"""游戏进程检测：进程名 + 窗口标题 双通道。"""
from __future__ import annotations

from dataclasses import dataclass

from . import winapi
from .games import Game


@dataclass
class Detection:
    pid: int
    exe_name: str          # 小写
    exe_path: str | None
    source: str            # "窗口" | "进程名"
    title: str = ""


def find_game(game: Game) -> Detection | None:
    procs = winapi.list_processes()
    if not procs:
        return None

    # 通道一：窗口标题（拿到的是"正在渲染的前台进程"，最准）
    for pid, title in winapi.list_visible_windows():
        low_title = title.lower()
        if not any(kw.lower() in low_title for kw in game.title_kw):
            continue
        exe = procs.get(pid, "").lower()
        if not exe or exe in game.exclude_exe:
            continue
        return Detection(pid, exe, winapi.image_path(pid), "窗口", title)

    # 通道二：进程名
    for pid, exe in procs.items():
        low = exe.lower()
        if low in game.exe_names and low not in game.exclude_exe:
            return Detection(pid, low, winapi.image_path(pid), "进程名")

    return None
