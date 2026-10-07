# -*- coding: utf-8 -*-
"""游戏模式盒入口。

双击 main.pyw（已关联 Python）或 启动游戏模式.cmd 即可运行。

命令行：
  （无参数）      图形界面
  --smoke        图形界面 + 打印 SMOKE_READY（供端到端测试等待）
  --selftest     跑逻辑自检（含负向测试），不显示窗口，退出码 0/1
  --gui-test     跑界面行为自检（会真实改系统设置并还原），退出码 0/1
  --e2e          端到端：子进程真实启动 → 外部观测 → WM_CLOSE 关闭 → 验证还原
  --version      打印版本
"""
import sys

# pythonw.exe（双击路径）下 sys.stdout/stderr 是 None，直接 reconfigure 会
# 启动即崩且无任何提示 —— 必须先判空（只有真实双击测试才暴露得了这个坑）。
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None:
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> int:
    argv = sys.argv[1:]
    from gmb import __version__

    if "--version" in argv:
        print(f"GameModeBox {__version__}")
        return 0
    if "--selftest" in argv:
        import selftest
        return selftest.run()
    if "--gui-test" in argv:
        import test_gui
        return test_gui.run()
    if "--e2e" in argv:
        import test_e2e
        return test_e2e.run()

    from gmb.app import run_gui
    return run_gui(smoke="--smoke" in argv)


if __name__ == "__main__":
    raise SystemExit(main())
