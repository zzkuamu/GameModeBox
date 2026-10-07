# -*- coding: utf-8 -*-
"""生成 assets/icon.ico（程序图标，供快捷方式/窗口用）。可重复运行。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gmb.app import make_ico  # noqa: E402

if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(root, "assets", "icon.ico")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    make_ico(out)
    print("written:", out, os.path.getsize(out), "bytes")
