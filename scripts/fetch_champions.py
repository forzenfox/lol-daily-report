#!/usr/bin/env python3
"""从 Riot Data Dragon 更新英雄中文名与全量头像。

用法：
    .venv\\Scripts\\python.exe scripts\\fetch_champions.py
    .venv\\Scripts\\python.exe scripts\\fetch_champions.py --no-avatars
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from champions import refresh_assets

DEFAULT_OUTPUT = Path("data/champions-zh_CN.json")
DEFAULT_AVATARS_DIR = Path("data/avatars")


def main() -> int:
    parser = argparse.ArgumentParser(description="更新英雄中文名与头像资源")
    parser.add_argument("--version", default=None, help="Data Dragon 版本，默认取最新")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--avatars-dir", default=str(DEFAULT_AVATARS_DIR))
    parser.add_argument("--no-avatars", action="store_true", help="只更新名称映射")
    args = parser.parse_args()

    result = refresh_assets(
        args.output,
        args.avatars_dir,
        version=args.version,
        download_avatars=not args.no_avatars,
    )
    print(f"已写入 {args.output}：{len(result.catalog)} 个英雄（{result.version}）")
    if not args.no_avatars:
        print(
            f"头像目录 {args.avatars_dir}：下载 {result.downloaded}，"
            f"已存在 {result.skipped}，失败 {result.failed}"
        )
        if result.failed:
            print("存在下载失败的头像，可重新运行本脚本补齐。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
