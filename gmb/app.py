"""游戏模式盒 主界面 + 托盘 + 控制器。

交互契约（按需求逐条落实）：
- 双击启动（.cmd / .pyw 关联）→ 有记忆游戏时自动进入游戏模式，没有则点卡片开启
- 点游戏卡片 = 选择该游戏并开启游戏模式；工具开着 = 游戏模式一直生效
- 「—」最小化 = 收进右下角任务栏（托盘），模式保持
- 标题栏「✕」/ 托盘「退出」 = 关闭游戏模式（全部还原）并退出
- 每个优化项在界面上给出真实回读结果，失败必须显示原因
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow,
    QMenu, QPushButton, QScrollArea, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from . import __version__, tweaks, winapi
from .detect import find_game
from .games import GAMES, GAMES_BY_KEY, Game
from .tweaks import Result, STORE

WINDOW_TITLE = "游戏模式盒"

# 运行日志：pythonw（双击路径）没有控制台，崩了什么都看不到 —— 必须落文件。
LOG_PATH = os.path.join(
    os.environ.get("GMB_LOG_DIR")
    or os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "run.log")


def _log(msg: str) -> None:
    import datetime
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


APP_QSS = """
QWidget { background:#f4f5f7; font-family:"Microsoft YaHei UI"; font-size:13px; color:#1f2328; }
QLabel#title { font-size:18px; font-weight:bold; }
QLabel#pill_on { background:#dcfce7; color:#15803d; border-radius:9px; padding:3px 10px; font-weight:bold; font-size:12px; }
QLabel#pill_off { background:#e8eaed; color:#6b7280; border-radius:9px; padding:3px 10px; font-weight:bold; font-size:12px; }
QLabel#section { color:#6b7280; font-size:12px; font-weight:bold; padding-top:8px; }
QLabel#lbl_detect { color:#374151; }
QFrame#panel { background:#ffffff; border:1px solid #e5e7eb; border-radius:10px; }
QPushButton#card { background:#ffffff; border:1px solid #e0e3e7; border-radius:10px;
  text-align:left; padding:2px 4px; font-size:13px; }
QPushButton#card:hover { border-color:#9ca3af; background:#fafbfc; }
QPushButton#card[selected="true"] { border:2px solid #16a34a; background:#f0fdf4; padding:1px 3px; }
QPushButton#big { background:#16a34a; color:white; border:none; border-radius:10px;
  padding:11px; font-weight:bold; font-size:14px; }
QPushButton#big:hover { background:#15803d; }
QPushButton#big:disabled { background:#d5d9de; color:#8b9198; }
QPushButton#foot { background:#ffffff; border:1px solid #d5d9de; border-radius:10px;
  padding:10px 14px; font-weight:bold; }
QPushButton#foot:hover { background:#f3f4f6; }
QMenu { background:#ffffff; border:1px solid #e0e3e7; padding:4px; }
QMenu::item { padding:6px 22px; border-radius:6px; }
QMenu::item:selected { background:#eef7f0; color:#15803d; }
QScrollArea > QWidget > QWidget { background:transparent; }
"""

CONFIG_PATH = os.environ.get("GMB_CONFIG") or os.path.join(
    tweaks.BASE_DIR, "config.json")
DEFAULT_CONFIG = {
    "last_game": None,
    "opts": {"mouse": True, "nagle": True, "suspend": False, "memtrim": False},
    "tray_hint_shown": False,
}


# ---------------------------------------------------------------- 配置（原子写）

def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            cfg.update(loaded)
            cfg["opts"] = {**DEFAULT_CONFIG["opts"], **loaded.get("opts", {})}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CONFIG_PATH)


# ---------------------------------------------------------------- 图标

def make_pixmap(on: bool, size: int = 64) -> QPixmap:
    """程序图标：圆角方块 + 白色 G。绿=运行中，灰=未开启。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#16a34a" if on else "#9aa0a6"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(2, 2, size - 4, size - 4, size * 0.24, size * 0.24)
    p.setPen(QPen(QColor("white")))
    f = QFont("Segoe UI", int(size * 0.52))
    f.setBold(True)
    p.setFont(f)
    p.drawText(0, 0, size, size, Qt.AlignCenter, "G")
    p.end()
    return pm


def make_ico(path: str) -> None:
    """生成 .ico（给快捷方式用）。QPixmap 依赖 QApplication，这里兜底自建。"""
    if QApplication.instance() is None:
        QApplication(sys.argv[:1])
    big = make_pixmap(True, 256)
    # Qt 的 ICO 写入器按单张图写；足够快捷方式使用
    big.save(path, "ICO")


# ---------------------------------------------------------------- 自绘开关（不依赖 QSS indicator）

class Switch(QAbstractButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setFixedSize(44, 24)
        self.setCursor(Qt.PointingHandCursor)

    # 注意：不要重写 sizeHint() 去调 self.fixedSize() —— QWidget 压根没有
    # fixedSize() 方法，在虚函数里抛 AttributeError 会让 PySide6 返回未初始化
    # 的 QSize，布局拿到野指针 → 段错误（本机实测 3 个以上开关必现）。
    # setFixedSize() 已经把 min/max 钉死，Qt 默认 sizeHint 会服从约束。

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.45)  # 禁用态（如非管理员的 Nagle 开关）视觉上要能看出来
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#16a34a") if self.isChecked() else QColor("#cfd4da"))
        p.drawRoundedRect(0, 0, 44, 24, 12, 12)
        p.setBrush(QColor("white"))
        cx = 32 if self.isChecked() else 12
        p.drawEllipse(cx - 9, 3, 18, 18)
        p.end()


# ---------------------------------------------------------------- 状态行文本

ICONS = {"ok": "✓", "fail": "✗", "pending": "⏳", "off": "—", "skip": "○"}


def _row_text(icon: str, color: str, name: str, detail: str) -> str:
    from html import escape
    return (f'<span style="color:{color};font-weight:bold">{icon}</span> '
            f'{escape(name)} <span style="color:#6b7280">· {escape(detail)}</span>')


# ---------------------------------------------------------------- 主窗口

class MainWindow(QMainWindow):
    # 后台线程完成优化应用/还原后通知主线程（跨线程信号，Qt 自动队列投递）
    op_done = Signal(str, dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(WINDOW_TITLE)
        self.setWindowIcon(QIcon(make_pixmap(True)))
        self.setStyleSheet(APP_QSS)
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            self.resize(470, min(780, geo.height() - 60))
        else:
            self.resize(470, 780)

        self.cfg = load_config()
        self.opts: dict = dict(self.cfg["opts"])
        self.mode_on = False
        self.game: Game | None = None
        self.detected = None
        self.results: dict[str, Result] = {}
        self._quitting = False
        # 异步状态机：None=空闲 | 'enabling' | 'disabling'
        self._phase: str | None = None
        self._pending_quit = False       # 还原完成后真正退出
        self._restoring_finished = False  # 放行 closeEvent 的关门闩

        self._build_ui()
        self.tray: QSystemTrayIcon | None = self._build_tray()
        self.op_done.connect(self._on_op_done)

        self.watch_timer = QTimer(self)
        self.watch_timer.setInterval(1500)
        self.watch_timer.timeout.connect(self._watch_tick)

        self.mem_timer = QTimer(self)
        self.mem_timer.setInterval(30_000)
        self.mem_timer.timeout.connect(self._mem_tick)

        # 启动自愈：上次被强杀留下的改动先还原
        heal = tweaks.heal_leftovers()
        if heal:
            _log("heal: " + " | ".join(heal))
            self._set_event("；".join(heal))

        # 记忆中的游戏 → 自动进入游戏模式（双击打开即游戏模式）
        last = self.cfg.get("last_game")
        _log(f"start: last_game={last!r} opts={self.opts}")
        if last in GAMES_BY_KEY:
            _log(f"auto-enable scheduled: {last} (+400ms)")
            QTimer.singleShot(400, lambda: self.enable_mode(last, auto=True))
        else:
            self._set_event("首次使用：点击上方游戏卡片即可开启游戏模式")
        self.refresh()

    # ---------------------------------------------------------------- UI

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        # 头部
        head = QHBoxLayout()
        title = QLabel(f"🎮 {WINDOW_TITLE}")
        title.setObjectName("title")
        head.addWidget(title)
        head.addStretch()
        self.pill = QLabel("未开启")
        self.pill.setObjectName("pill_off")
        head.addWidget(self.pill)
        root.addLayout(head)

        sub = QLabel("点游戏卡片 = 开启游戏模式；工具开着就一直生效，退出自动全部还原")
        sub.setWordWrap(True)
        sub.setStyleSheet("color:#6b7280;font-size:12px;")
        root.addWidget(sub)

        # 游戏卡片：按钮本体负责点击/焦点/QSS，文字用内嵌 QLabel 渲染富文本。
        # 实测：挂样式表的 QPushButton 会把 HTML 源码原样画出来（setTextFormat
        # 也救不了），而 QLabel 的富文本在本机是可靠的（状态行就是这么渲染的）。
        self.cards: dict[str, QPushButton] = {}
        self.card_hints: dict[str, QLabel] = {}
        self.card_names: dict[str, QLabel] = {}
        for g in GAMES:
            card = QPushButton()
            card.setObjectName(f"btn_game_{g.key}")
            card.setProperty("selected", False)
            card.setFixedHeight(58)
            card.setCursor(Qt.PointingHandCursor)
            lay = QVBoxLayout(card)
            lay.setContentsMargins(12, 8, 12, 8)
            lay.setSpacing(1)
            name_lbl = QLabel()
            name_lbl.setObjectName(f"lbl_game_{g.key}")
            hint_lbl = QLabel()
            hint_lbl.setObjectName(f"lbl_hint_{g.key}")
            for lbl in (name_lbl, hint_lbl):
                lbl.setAttribute(Qt.WA_TransparentForMouseEvents)  # 点击穿透到按钮
                lbl.setStyleSheet("background:transparent;border:none;")
            lay.addWidget(name_lbl)
            lay.addWidget(hint_lbl)
            card.clicked.connect(lambda _=False, k=g.key: self._on_card(k))
            root.addWidget(card)
            self.cards[g.key] = card
            self.card_names[g.key] = name_lbl
            self.card_hints[g.key] = hint_lbl

        # 滚动内容区：优化项
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        scroll.viewport().setStyleSheet("background:transparent;")
        body = QWidget()
        body.setStyleSheet("background:transparent;")
        self.body_layout = QVBoxLayout(body)
        self.body_layout.setContentsMargins(0, 4, 0, 0)
        self.body_layout.setSpacing(6)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        # 正在生效分区
        self.body_layout.addWidget(self._section("正在生效"))
        self.row_labels: dict[str, QLabel] = {}
        self.row_order = ["power", "gamemode", "gamedvr", "priority", "fullscreen"]
        for tid in self.row_order:
            lbl = QLabel()
            lbl.setObjectName(f"row_{tid}")
            lbl.setWordWrap(True)
            self.body_layout.addWidget(lbl)
            self.row_labels[tid] = lbl

        # 可选开关分区
        self.body_layout.addWidget(self._section("可选优化（开关）"))
        self.opt_rows: dict[str, tuple[QLabel, Switch]] = {}
        for t in tweaks.TOGGLEABLE:
            self.opt_rows[t.id] = self._opt_row(t)

        # 检测状态
        self.body_layout.addWidget(self._section("游戏检测"))
        self.lbl_detect = QLabel("未开启游戏模式")
        self.lbl_detect.setObjectName("lbl_detect")
        self.lbl_detect.setWordWrap(True)
        self.body_layout.addWidget(self.lbl_detect)

        self.lbl_event = QLabel("")
        self.lbl_event.setWordWrap(True)
        self.lbl_event.setStyleSheet("color:#9ca3af;font-size:11px;")
        self.body_layout.addWidget(self.lbl_event)
        self.body_layout.addStretch()

        # 底部按钮
        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.btn_toggle = QPushButton("关闭游戏模式")
        self.btn_toggle.setObjectName("big")
        self.btn_toggle.clicked.connect(self.disable_mode)
        self.btn_hide = QPushButton("收进托盘 ▾")
        self.btn_hide.setObjectName("foot")
        self.btn_hide.clicked.connect(self._hide_to_tray)
        foot.addWidget(self.btn_toggle, 1)
        foot.addWidget(self.btn_hide)
        root.addLayout(foot)

    def _section(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("section")
        return lbl

    def _opt_row(self, t) -> tuple[QLabel, Switch]:
        frame = QFrame()
        frame.setObjectName("panel")
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(10, 6, 10, 6)
        col = QVBoxLayout()
        col.setSpacing(1)
        name = QLabel(t.name)
        name.setStyleSheet("font-weight:bold;")
        desc = QLabel(t.desc)
        desc.setStyleSheet("color:#6b7280;font-size:11px;")
        desc.setWordWrap(True)
        col.addWidget(name)
        col.addWidget(desc)
        lay.addLayout(col, 1)
        sw = Switch()
        sw.setChecked(bool(self.opts.get(t.id, t.default_opt)))
        sw.toggled.connect(lambda checked, tid=t.id: self._on_opt(tid, checked))
        lay.addWidget(sw, 0, Qt.AlignVCenter)
        self.row_labels[f"opt_{t.id}"] = desc  # 状态详情复用 desc 行尾展示
        self.body_layout.addWidget(frame)
        return name, sw  # type: ignore[return-value]

    # ---------------------------------------------------------------- 托盘

    def _build_tray(self) -> QSystemTrayIcon | None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        tray = QSystemTrayIcon(QIcon(make_pixmap(False)), self)
        tray.setToolTip(f"{WINDOW_TITLE} · 未开启")
        menu = QMenu()
        self.act_open = QAction("打开主界面", menu)
        self.act_open.triggered.connect(self._show_from_tray)
        self.act_toggle = QAction("开启游戏模式", menu)
        self.act_toggle.triggered.connect(self._tray_toggle)
        self.act_quit = QAction("退出（还原并退出）", menu)
        self.act_quit.triggered.connect(self.shutdown)
        menu.addAction(self.act_open)
        menu.addSeparator()
        menu.addAction(self.act_toggle)
        menu.addSeparator()
        menu.addAction(self.act_quit)
        tray.setContextMenu(menu)
        tray.activated.connect(
            lambda reason: self._show_from_tray()
            if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick)
            else None)
        tray.messageClicked.connect(self._show_from_tray)
        tray.show()
        return tray

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _hide_to_tray(self) -> None:
        if self.tray is None:
            self.showMinimized()
            return
        self.hide()
        if not self.cfg.get("tray_hint_shown"):
            self.cfg["tray_hint_shown"] = True
            save_config(self.cfg)
            self.tray.showMessage(
                WINDOW_TITLE,
                "已收进任务栏，游戏模式保持开启；右键托盘图标可退出（自动还原）",
                QIcon(make_pixmap(True)), 4000)

    def _tray_toggle(self) -> None:
        if self._phase is not None:
            return
        if self.mode_on:
            self.disable_mode()
        elif self.game is not None:
            self.enable_mode(self.game.key)

    def changeEvent(self, e) -> None:  # noqa: N802
        # 点「—」= 收进托盘（而不是最小化到任务栏）
        if e.type() == QEvent.WindowStateChange and self.isMinimized():
            QTimer.singleShot(0, self._hide_to_tray)
        super().changeEvent(e)

    # ---------------------------------------------------------------- 业务

    def _on_card(self, key: str) -> None:
        if self._phase is not None:
            return
        if self.mode_on and self.game and self.game.key == key:
            return
        self.enable_mode(key)

    def enable_mode(self, key: str, auto: bool = False) -> None:
        game = GAMES_BY_KEY.get(key)
        if game is None:
            _log(f"enable_mode: unknown game key {key!r}")
            return
        if self._phase is not None:
            return  # 正在开启/还原中，忽略新请求
        if self.mode_on and self.game and self.game.key == key:
            return
        if self.mode_on:
            self._switch_game(game)
            return
        _log(f"enable_mode: key={key} auto={auto}")

        self.game = game
        self.cfg["last_game"] = key
        self.cfg["opts"] = dict(self.opts)
        save_config(self.cfg)

        # 状态先翻，重活进后台线程：powercfg 在 pythonw 下单次可达秒级，
        # 在 UI 线程同步跑会让窗口"未响应"（实测首调 5.7s、还原 6.1s）
        self.mode_on = True
        self.detected = None
        self._phase = "enabling"
        self.results = {t.id: Result(True, "应用中…")
                        for t in tweaks.ALWAYS + tweaks.TOGGLEABLE + tweaks.RUNTIME}
        self._set_event(("自动开启" if auto else "已开启") +
                        f"游戏模式 · 目标：{game.name}，正在应用优化…")
        self.refresh()
        opts = dict(self.opts)

        def work() -> None:
            res: dict[str, Result] = {}
            for t in tweaks.ALWAYS:
                res[t.id] = t.apply()
                _log(f"enable {t.id}: ok={res[t.id].ok} {res[t.id].detail}")
            for t in tweaks.TOGGLEABLE:
                if not opts.get(t.id, t.default_opt):
                    res[t.id] = Result(True, "未启用（开关关闭）")
                elif t.requires_admin and not winapi.is_admin():
                    # 非管理员不白试：灰色跳过，不报红吓人（管理员运行时会真生效）
                    res[t.id] = Result(False, "需要管理员权限，已跳过（可选）")
                else:
                    res[t.id] = t.apply()
                    _log(f"enable {t.id}: ok={res[t.id].ok} {res[t.id].detail}")
            self.op_done.emit("enable", {"results": res, "auto": auto})

        threading.Thread(target=work, name="gmb-enable", daemon=True).start()

    def _switch_game(self, game: Game) -> None:
        assert self.game is not None
        old = self.game.name
        self.game = game
        self.cfg["last_game"] = game.key
        save_config(self.cfg)
        # 撤掉针对旧游戏的运行期改动（都是轻量操作，同步即可），
        # 等新游戏进程出现再重新上
        r_prio = tweaks.priority.revert()
        r_fs = tweaks.fullscreen.revert()
        self.results["priority"] = Result(r_prio.ok, "等待游戏启动…")
        self.results["fullscreen"] = Result(r_fs.ok, "等待游戏启动…")
        self.detected = None
        self._set_event(f"切换目标：{old} → {game.name}")
        self.refresh()
        if self.tray:
            self.tray.showMessage(WINDOW_TITLE, f"已切换目标：{game.name}",
                                  QIcon(make_pixmap(True)), 2500)

    def _on_op_done(self, op: str, payload: dict) -> None:
        """后台线程完成 → 回到 UI 线程收尾。"""
        if op == "enable":
            self.results.update(payload["results"])
            self.results["priority"] = Result(True, "等待游戏启动…")
            self.results["fullscreen"] = Result(True, "等待游戏启动…")
            self._phase = None
            self.watch_timer.start()
            if self.opts.get("memtrim"):
                self._mem_tick()
                self.mem_timer.start()
            fails = [t.name for t in tweaks.ALWAYS if not self.results[t.id].ok]
            self._set_event(
                ("自动开启" if payload.get("auto") else "已开启") +
                f"游戏模式 · 目标：{self.game.name if self.game else '?'}" +
                (f"（{len(fails)} 项未生效：{'、'.join(fails)}）" if fails else ""))
            if self.tray:
                self.tray.showMessage(
                    WINDOW_TITLE, f"游戏模式已开启 · {self.game.name}",
                    QIcon(make_pixmap(True)), 2500)
            if self._pending_quit:
                # 开启途中用户点了 X：现在接力还原并退出
                self._start_restore(quit_when_done=True)
            self.refresh()
        elif op == "disable":
            self.results = payload["results"]
            self.mode_on = False
            self.detected = None
            self._phase = None
            details = payload.get("details", [])
            event = "游戏模式已关闭，所有修改已还原"
            if details:
                event += "（注意：" + "；".join(details) + "）"
            self._set_event(event)
            if payload.get("quit"):
                self._final_close()
            else:
                if self.tray:
                    self.tray.showMessage(WINDOW_TITLE, "游戏模式已关闭，设置已还原",
                                          QIcon(make_pixmap(False)), 2500)
                self.refresh()

    def _start_restore(self, quit_when_done: bool) -> None:
        """异步还原：UI 线程只调度，重活在后台线程。
        若进程在还原途中被强杀 → state.json 里仍有账，下次启动 heal 自愈。"""
        if self._phase == "disabling":
            if quit_when_done:
                self._pending_quit = True
            return
        self._phase = "disabling"
        self.watch_timer.stop()
        self.mem_timer.stop()
        if quit_when_done:
            self._pending_quit = True
        self._set_event("正在还原所有修改…")
        self.refresh()

        def work() -> None:
            results: dict[str, Result] = {}
            details: list[str] = []
            # 还原顺序：运行期 → 可选 → 必开（倒序）
            order = [tweaks.suspend, tweaks.priority, tweaks.fullscreen] + \
                [t for t in reversed(tweaks.TOGGLEABLE)
                 if t is not tweaks.suspend] + \
                list(reversed(tweaks.ALWAYS))
            seen: set[str] = set()
            for t in order:
                if t.id in seen:
                    continue
                seen.add(t.id)
                res = t.revert()
                results[t.id] = res
                _log(f"disable {t.id}: ok={res.ok} {res.detail}")
                if not res.ok:
                    details.append(f"{t.name}: {res.detail}")
            for t in tweaks.ALWAYS + tweaks.TOGGLEABLE + tweaks.RUNTIME:
                results.setdefault(t.id, Result(True, "无记录"))
            self.op_done.emit("disable", {"results": results, "details": details,
                                          "quit": quit_when_done or self._pending_quit})

        threading.Thread(target=work, name="gmb-restore", daemon=True).start()

    def disable_mode(self) -> None:
        """关闭游戏模式（工具保持打开）。"""
        if self._phase is not None:
            return
        if not self.mode_on:
            return
        self._start_restore(quit_when_done=False)

    def shutdown(self) -> None:
        """托盘「退出」：先异步还原，完成后再退出。"""
        self.request_quit()

    def request_quit(self) -> None:
        if self._restoring_finished:
            return
        self._pending_quit = True
        if self._phase == "disabling":
            return  # 还原进行中，完成回调会退出
        if self._phase == "enabling":
            return  # 开启进行中，完成后接力还原（见 _on_op_done）
        if self.mode_on:
            self._start_restore(quit_when_done=True)
        else:
            self._final_close()

    def _final_close(self) -> None:
        _log("final close: restore finished, quitting")
        self._restoring_finished = True
        self._quitting = True
        self.close()

    # ---------------------------------------------------------------- 可选开关（在线热切换）

    def _on_opt(self, tid: str, checked: bool) -> None:
        self.opts[tid] = checked
        self.cfg["opts"] = dict(self.opts)
        save_config(self.cfg)
        t = next(x for x in tweaks.TOGGLEABLE if x.id == tid)
        if self._phase is not None or not self.mode_on:
            # 开启/还原进行中只记偏好，不并发动系统
            self.refresh()
            return
        if checked:
            self.results[tid] = t.apply()
            if tid == "memtrim":
                self._mem_tick()
                self.mem_timer.start()
        else:
            if tid == "memtrim":
                self.mem_timer.stop()
            self.results[tid] = t.revert()
        self.refresh()

    # ---------------------------------------------------------------- 检测与运行期优化

    def _watch_tick(self) -> None:
        if self._phase is not None or not self.mode_on or self.game is None:
            return
        det = find_game(self.game)
        if det is None:
            if self.detected is not None:
                self.detected = None
                self.results["priority"] = Result(True, "等待游戏启动…")
                self._set_event("游戏已退出，优化保持待命")
                self.refresh()
            self.lbl_detect.setText("⏳ 等待游戏启动…（系统级优化已生效）")
            return
        if self.detected and self.detected.pid == det.pid:
            return
        self.detected = det
        self.results["priority"] = tweaks.priority.apply_for_pid(det.pid, det.exe_name)
        if det.exe_path:
            self.results["fullscreen"] = tweaks.fullscreen.apply_for_exe(det.exe_path)
        else:
            self.results["fullscreen"] = Result(False, "读不到游戏 exe 路径")
        self.lbl_detect.setText(
            f"✓ 已检测 · {self.game.name} · {det.exe_name} · PID {det.pid}（{det.source}通道）")
        self.refresh()

    def _mem_tick(self) -> None:
        if not self.mode_on or not self.opts.get("memtrim"):
            return
        self.results["memtrim"] = tweaks.memtrim.run_once()
        self.refresh()

    # ---------------------------------------------------------------- 刷新显示

    def _set_event(self, text: str) -> None:
        self.lbl_event.setText("最近：" + text)

    def refresh(self) -> None:
        import html

        # 头部状态
        on = self.mode_on
        if self._phase == "enabling":
            pill_text, pill_obj = "开启中…", "pill_off"
        elif self._phase == "disabling":
            pill_text, pill_obj = "还原中…", "pill_off"
        else:
            pill_text = "运行中" if on else "未开启"
            pill_obj = "pill_on" if on else "pill_off"
        self.pill.setText(pill_text)
        self.pill.setObjectName(pill_obj)
        self.pill.style().unpolish(self.pill)
        self.pill.style().polish(self.pill)

        # 卡片（开启/还原进行中禁点，防并发重入）
        busy = self._phase is not None
        for key, card in self.cards.items():
            g = GAMES_BY_KEY[key]
            selected = bool(on and self.game and self.game.key == key)
            card.setProperty("selected", selected)
            card.setEnabled(not busy)
            card.style().unpolish(card)
            card.style().polish(card)
            if selected:
                hint = "✔ 游戏模式目标"
            elif on:
                hint = "点击切换到这款游戏"
            else:
                hint = "点击开启游戏模式"
            self.card_names[key].setText(
                f'<span style="font-size:15px;font-weight:bold">'
                f'{g.emoji}&nbsp;&nbsp;{g.name}</span>')
            self.card_hints[key].setText(
                f'<span style="color:#6b7280;font-size:11px">{hint}</span>')

        # 大按钮
        self.btn_toggle.setEnabled(on and not busy)
        if self._phase == "disabling":
            self.btn_toggle.setText("正在还原…")
        elif self._phase == "enabling":
            self.btn_toggle.setText("正在开启…")
        else:
            self.btn_toggle.setText("关闭游戏模式（还原全部设置）" if on
                                    else "游戏模式未开启")
        if self.tray:
            self.tray.setToolTip(
                f"{WINDOW_TITLE} · {'运行中 · ' + (self.game.name if self.game else '')}"
                if on else f"{WINDOW_TITLE} · 未开启")
            self.tray.setIcon(QIcon(make_pixmap(on)))
            if hasattr(self, "act_toggle"):
                self.act_toggle.setText("关闭游戏模式" if on else "开启游戏模式")

        # 状态行
        by_id = {t.id: t for t in tweaks.ALL}
        for tid, lbl in self.row_labels.items():
            if tid.startswith("opt_"):
                continue
            t = by_id[tid]
            if tid in self.results:
                r = self.results[tid]
                if "需要管理员" in r.detail:
                    icon, color = ICONS["off"], "#9ca3af"  # 权限不足=灰色跳过，不是错误
                elif r.ok:
                    icon, color = ICONS["ok"], "#16a34a"
                    if "等待" in r.detail or "无记录" in r.detail:
                        icon, color = ICONS["pending"], "#d97706"
                        if r.detail == "无记录":
                            icon, color = ICONS["off"], "#9ca3af"
                else:
                    icon, color = ICONS["fail"], "#dc2626"
                detail = r.detail
            else:
                icon, color, detail = ICONS["off"], "#9ca3af", "未生效"
            lbl.setText(_row_text(icon, color, t.name, detail))

        # 可选行详情
        for t in tweaks.TOGGLEABLE:
            name_lbl, sw = self.opt_rows[t.id]
            sw.blockSignals(True)
            sw.setChecked(bool(self.opts.get(t.id, t.default_opt)))
            sw.blockSignals(False)
            if t.requires_admin and not winapi.is_admin():
                sw.setEnabled(False)
                name_lbl.setText(f'{t.name} <span style="color:#b45309">（需管理员）</span>')
            else:
                sw.setEnabled(True)
                name_lbl.setText(t.name)
            r = self.results.get(t.id)
            if r is not None:
                if "未启用" in r.detail or "无记录" in r.detail or "需要管理员" in r.detail:
                    color = "#9ca3af"
                else:
                    color = "#16a34a" if r.ok else "#dc2626"
                self.row_labels[f"opt_{t.id}"].setText(
                    f'{html.escape(t.desc)} <span style="color:{color}">· {html.escape(r.detail)}</span>')
            else:
                self.row_labels[f"opt_{t.id}"].setText(html.escape(t.desc))

        # 检测行
        if self._phase == "enabling":
            self.lbl_detect.setText("⏳ 优化应用中…")
        elif self._phase == "disabling":
            self.lbl_detect.setText("⏳ 正在还原…")
        elif on:
            if self.detected:
                pass  # _watch_tick 已写入
            else:
                self.lbl_detect.setText("⏳ 等待游戏启动…（系统级优化已生效）")
        else:
            self.lbl_detect.setText("未开启游戏模式")

    # ---------------------------------------------------------------- 关闭 = 关闭游戏模式

    def closeEvent(self, e: QCloseEvent) -> None:  # noqa: N802
        _log(f"closeEvent: mode_on={self.mode_on} phase={self._phase} "
             f"finished={self._restoring_finished} pending_quit={self._pending_quit}")
        if self._restoring_finished:
            e.accept()
            return
        if self._phase == "disabling":
            # 还原进行中点 X：记下"完成后退出"，不打断还原
            self._pending_quit = True
            e.ignore()
            return
        if not self.mode_on:
            e.accept()  # 已关闭状态，无残留账（disable 完成时已清）
            return
        # 需要还原：先拦下关闭，异步还原完成 → _final_close() 放行
        e.ignore()
        self.request_quit()


# ---------------------------------------------------------------- 入口

def _focus_existing() -> bool:
    """单实例：找到已有窗口并显示到前台。"""
    hwnd = winapi.user32.FindWindowW(None, WINDOW_TITLE)
    if not hwnd:
        return False
    SW_RESTORE, SW_SHOW = 9, 5
    winapi.user32.ShowWindow(hwnd, SW_RESTORE)
    winapi.user32.ShowWindow(hwnd, SW_SHOW)
    winapi.user32.SetForegroundWindow(hwnd)
    return True


def run_gui(smoke: bool = False) -> int:
    from PySide6.QtCore import QLockFile

    # pythonw 下未捕获异常默认无声无息 —— 一律落日志
    def _excepthook(tp, val, tb):
        import traceback
        _log("UNCAUGHT: " + "".join(traceback.format_exception(tp, val, tb)))
        sys.__excepthook__(tp, val, tb)

    sys.excepthook = _excepthook

    app = QApplication(sys.argv)
    app.setApplicationName(WINDOW_TITLE)
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setWindowIcon(QIcon(make_pixmap(True)))

    lock = QLockFile(os.path.join(tempfile.gettempdir(), "GameModeBox.lock"))
    if not lock.tryLock(100):
        _focus_existing()
        print("已有实例在运行，已将前台窗口唤起", flush=True)
        return 0

    win = MainWindow()
    win.show()
    if smoke:
        QTimer.singleShot(400, lambda: print("SMOKE_READY", flush=True))
    rc = app.exec()
    lock.unlock()
    return rc
