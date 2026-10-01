#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""国漫数据采集 —— 命令行入口。

    python run.py doctor     环境体检
    python run.py sync       采集
    python run.py export     导出 Tableau 宽表

`python run.py -h` 看全部命令。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 让脚本能从任意目录被调用
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Windows 控制台默认 GBK，打印中文/符号会炸，这里强制 UTF-8
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from animedata.pipeline import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
