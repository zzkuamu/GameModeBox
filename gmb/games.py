"""三款游戏的识别档案。

exe_names：进程文件名候选（小写匹配）；来源见 README「识别依据」。
title_kw ：窗口标题关键字 —— 兜底通道，即使 exe 名对不上也能靠窗口认出游戏。
exclude  ：绝不提权/绝不挂起的进程（启动器、反作弊服务）。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Game:
    key: str
    name: str
    emoji: str
    exe_names: tuple[str, ...]
    title_kw: tuple[str, ...]
    exclude_exe: tuple[str, ...] = field(default=())


GAMES: list[Game] = [
    Game(
        key="delta",
        name="三角洲行动",
        emoji="⚔️",
        exe_names=("deltaforce.exe",),
        title_kw=("三角洲行动", "delta force"),
    ),
    Game(
        key="abi",
        name="暗区突围无限",
        emoji="🎒",
        # UAGame.exe 来自 SmoothWizard 目录（UE5 工程默认名）；
        # 其余为常见打包名，任何一条命中即可，窗口标题兜底。
        exe_names=(
            "uagame.exe",
            "abinfinite.exe",
            "arenabreakoutinfinite.exe",
            "arena breakout infinite.exe",
        ),
        title_kw=("暗区突围", "arena breakout"),
    ),
    Game(
        key="valorant",
        name="无畏契约",
        emoji="🎯",
        # VALORANT-Win64-Shipping.exe 由 OpenGameBoost 标注 Verified: Riot/Unreal
        exe_names=("valorant-win64-shipping.exe",),
        title_kw=("valorant", "无畏契约"),
        exclude_exe=(
            "riotclientux.exe",
            "riotclientservices.exe",
            "vgc.exe",
            "vgtray.exe",
        ),
    ),
]

GAMES_BY_KEY: dict[str, Game] = {g.key: g for g in GAMES}
