# -*- coding: utf-8 -*-
"""演示数据：3 条短链 + 最近 14 天的点击。

被两处复用：
1. scripts/seed_demo.py（本地手动灌数据）
2. app.py（部署到云端时，空库自动灌一份，免得演示页面是空的）
"""
from __future__ import annotations

import random
from datetime import timedelta

from store import Store, iso, utcnow

REFERERS = [
    "", "", "",
    "https://mp.weixin.qq.com/",
    "https://www.xiaohongshu.com/",
    "https://www.zhihu.com/",
    "https://weibo.com/",
]
UAS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Mozilla/5.0 (Linux; Android 14)",
]

LINKS = [
    {
        "slug": "spring-campaign",
        "variants": [
            {"url": "https://example.com/landing?a", "weight": 2, "label": "A"},
            {"url": "https://example.com/landing?b", "weight": 1, "label": "B"},
        ],
        "days": 14,
        "base": 12,      # 每天基准点击数
        "trend": 1.6,    # 越靠近今天越多
    },
    {
        "slug": "resume-v2",
        "variants": [{"url": "https://example.com/resume", "weight": 1, "label": "默认"}],
        "days": 14,
        "base": 3,
        "trend": 1.0,
    },
    {
        "slug": "event-1120",
        "variants": [{"url": "https://example.com/event", "weight": 1, "label": "默认"}],
        "days": 10,
        "base": 5,
        "trend": 0.6,
    },
]


def seed(store: Store, seed_value: int = 20261107) -> int:
    """写入演示数据，返回写入的点击总数。"""
    rng = random.Random(seed_value)
    total = 0

    for spec in LINKS:
        if store.exists(spec["slug"]):
            store.delete(spec["slug"])
        store.create(spec["slug"], spec["variants"], None, owner="demo")
        link = store.get(spec["slug"])

        for day_offset in range(spec["days"]):
            day = utcnow() - timedelta(days=spec["days"] - 1 - day_offset)
            weight = 1 + (spec["trend"] - 1) * (day_offset / max(1, spec["days"] - 1))
            weekend = 0.55 if day.weekday() >= 5 else 1.0
            n = max(0, int(rng.gauss(spec["base"] * weight * weekend, 3)))
            for _ in range(n):
                variant = store.pick_variant(link)
                ts = day.replace(
                    hour=rng.randint(8, 23), minute=rng.randint(0, 59),
                    second=rng.randint(0, 59), microsecond=0)
                with store._lock:  # noqa: SLF001  演示数据，直接写库更快
                    store._conn.execute(
                        "INSERT INTO clicks(slug, variant_id, ts, referer, ua, ip_hash) "
                        "VALUES (?,?,?,?,?,?)",
                        (spec["slug"], variant["id"], iso(ts),
                         rng.choice(REFERERS), rng.choice(UAS), "demo0000demo0000"),
                    )
                total += 1

        with store._lock:  # noqa: SLF001
            store._conn.execute(
                "UPDATE links SET hits = (SELECT COUNT(*) FROM clicks WHERE slug = ?) WHERE slug = ?",
                (spec["slug"], spec["slug"]))
            store._conn.commit()

    return total
