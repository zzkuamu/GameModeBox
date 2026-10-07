# -*- coding: utf-8 -*-
"""生成双击启动脚本（.cmd）。

铁律：内容必须纯 ASCII + CRLF —— cmd.exe 按系统 ANSI(GBK) 解析批处理，
中文注释会乱码且 LF 行尾下乱码尾部会吞掉下一行命令（本机 100% 复现的坑）。
中文只能出现在文件名里。
"""
import os

LAUNCH = """@echo off
rem GameModeBox launcher - ASCII only + CRLF on purpose (cmd.exe parses .cmd as ANSI)
set "HERE=%~dp0"
if not exist "%HERE%main.pyw" (
  echo [ERROR] main.pyw not found next to this script
  pause
  exit /b 1
)
rem Resolve Python via the .pyw file association (pyw.exe -> the Python that
rem has PySide6). Do NOT use "where pythonw" here: a PySide6-less pythonw.exe
rem can sit earlier in PATH and the tool would die at import.
start "" "%HERE%main.pyw"
exit /b 0
"""

ADMIN = """@echo off
rem GameModeBox launcher (Administrator) - ASCII only + CRLF on purpose
set "HERE=%~dp0"
if not exist "%HERE%main.pyw" (
  echo [ERROR] main.pyw not found next to this script
  pause
  exit /b 1
)
rem UAC elevation so the optional Nagle network tweak can apply.
rem If the project path contains spaces, edit the ArgumentList line below.
{PS} -NoProfile -Command "Start-Process -FilePath 'C:\\Windows\\pyw.exe' -ArgumentList '%HERE%main.pyw' -Verb RunAs"
if errorlevel 1 (
  echo [ERROR] UAC prompt failed or was cancelled
  pause
  exit /b 1
)
exit /b 0
""".replace("{PS}", "powershell")


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name, text in (("启动游戏模式.cmd", LAUNCH),
                       ("启动游戏模式(管理员).cmd", ADMIN)):
        data = text.replace("\n", "\r\n").encode("ascii")  # ASCII only + CRLF
        assert all(b < 128 for b in data), "non-ASCII byte!"
        assert b"\n" not in data.replace(b"\r\n", b""), "lone LF!"
        path = os.path.join(root, name)
        with open(path, "wb") as f:
            f.write(data)
        print(f"written: {name} ({len(data)} bytes, CRLF={data.count(b'chr') or True})")


if __name__ == "__main__":
    main()
