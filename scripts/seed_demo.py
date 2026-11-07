# -*- coding: utf-8 -*-
"""本地灌演示数据：python scripts/seed_demo.py"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from demo import seed  # noqa: E402
from store import Store  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path(__file__).resolve().parent.parent / "data" / "splitlink.db"))
    args = ap.parse_args()

    store = Store(args.db)
    total = seed(store)
    for spec in store.list_links():
        print(f"  /{spec['slug']}: {spec['hits']} 次点击")
    print(f"完成，共 {total} 次点击 -> {args.db}")
    store.close()


if __name__ == "__main__":
    main()
