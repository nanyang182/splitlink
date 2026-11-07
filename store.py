# -*- coding: utf-8 -*-
"""SQLite 存储层：建表、写入、聚合。

单文件数据库 + WAL，够几百 QPS 的演示场景；
真要上量，这一层换成 Postgres 只需要改 SQL。
"""
from __future__ import annotations

import random
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS links (
    slug        TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    expires_at  TEXT,
    owner       TEXT,
    hits        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS variants (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    slug    TEXT NOT NULL REFERENCES links(slug) ON DELETE CASCADE,
    label   TEXT NOT NULL,
    target  TEXT NOT NULL,
    weight  INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_variants_slug ON variants(slug);

CREATE TABLE IF NOT EXISTS clicks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT NOT NULL,
    variant_id  INTEGER NOT NULL,
    ts          TEXT NOT NULL,
    referer     TEXT,
    ua          TEXT,
    ip_hash     TEXT
);

CREATE INDEX IF NOT EXISTS idx_clicks_slug_ts ON clicks(slug, ts);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ 写入
    def create(self, slug: str, variants: list[dict], expires_in_days: int | None,
               owner: str | None = None) -> dict:
        now = utcnow()
        expires = iso(now + timedelta(days=expires_in_days)) if expires_in_days else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO links(slug, created_at, expires_at, owner) VALUES (?,?,?,?)",
                (slug, iso(now), expires, owner),
            )
            self._conn.executemany(
                "INSERT INTO variants(slug, label, target, weight) VALUES (?,?,?,?)",
                [(slug, v["label"], v["url"], v["weight"]) for v in variants],
            )
            self._conn.commit()
        return self.get(slug)  # type: ignore[return-value]

    def delete(self, slug: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM links WHERE slug = ?", (slug,))
            self._conn.execute("DELETE FROM variants WHERE slug = ?", (slug,))
            self._conn.execute("DELETE FROM clicks WHERE slug = ?", (slug,))
            self._conn.commit()
        return cur.rowcount > 0

    def record_click(self, slug: str, variant_id: int, referer: str | None,
                     ua: str | None, ip_hash: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO clicks(slug, variant_id, ts, referer, ua, ip_hash) VALUES (?,?,?,?,?,?)",
                (slug, variant_id, iso(utcnow()), referer, (ua or "")[:200], ip_hash),
            )
            self._conn.execute("UPDATE links SET hits = hits + 1 WHERE slug = ?", (slug,))
            self._conn.commit()

    # ------------------------------------------------------------ 读取
    def exists(self, slug: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM links WHERE slug = ?", (slug,)).fetchone()
        return row is not None

    def get(self, slug: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM links WHERE slug = ?", (slug,)).fetchone()
            if row is None:
                return None
            variants = self._conn.execute(
                "SELECT id, label, target, weight FROM variants WHERE slug = ? ORDER BY id",
                (slug,),
            ).fetchall()
        return {
            "slug": row["slug"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "owner": row["owner"],
            "hits": row["hits"],
            "variants": [dict(v) for v in variants],
        }

    def is_expired(self, link: dict) -> bool:
        exp = link.get("expires_at")
        if not exp:
            return False
        return datetime.fromisoformat(exp.replace("Z", "+00:00")) <= utcnow()

    @staticmethod
    def pick_variant(link: dict) -> dict:
        """按权重随机分流；随机数来自 random，压力测试时可注入固定种子。"""
        variants = link["variants"]
        total = sum(v["weight"] for v in variants)
        point = random.uniform(0, total)
        upto = 0.0
        for v in variants:
            upto += v["weight"]
            if point <= upto:
                return v
        return variants[-1]

    def list_links(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT slug, created_at, expires_at, hits FROM links ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self, slug: str, days: int = 14) -> dict | None:
        with self._lock:
            link = self.get(slug)
            if link is None:
                return None
            total = link["hits"]
            by_variant = self._conn.execute(
                """
                SELECT v.id, v.label, v.target, v.weight,
                       COUNT(c.id) AS clicks
                FROM variants v
                LEFT JOIN clicks c ON c.variant_id = v.id
                WHERE v.slug = ?
                GROUP BY v.id ORDER BY v.id
                """,
                (slug,),
            ).fetchall()
            since = iso(utcnow() - timedelta(days=days - 1))[:10]
            daily_rows = self._conn.execute(
                "SELECT substr(ts,1,10) AS d, COUNT(*) AS n FROM clicks "
                "WHERE slug = ? AND substr(ts,1,10) >= ? GROUP BY d",
                (slug, since),
            ).fetchall()
            referers = self._conn.execute(
                "SELECT COALESCE(NULLIF(referer,''),'直接访问') AS r, COUNT(*) AS n "
                "FROM clicks WHERE slug = ? GROUP BY r ORDER BY n DESC LIMIT 6",
                (slug,),
            ).fetchall()
            recent = self._conn.execute(
                "SELECT c.ts, v.label, COALESCE(NULLIF(c.referer,''),'直接访问') AS referer "
                "FROM clicks c JOIN variants v ON v.id = c.variant_id "
                "WHERE c.slug = ? ORDER BY c.ts DESC LIMIT 8",
                (slug,),
            ).fetchall()

        daily_map = {r["d"]: r["n"] for r in daily_rows}
        series = []
        for i in range(days - 1, -1, -1):
            d = (utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
            series.append({"date": d, "clicks": daily_map.get(d, 0)})

        variants = [
            {
                "id": r["id"],
                "label": r["label"],
                "target": r["target"],
                "weight": r["weight"],
                "clicks": r["clicks"],
                "share": round(r["clicks"] / total * 100, 1) if total else 0.0,
            }
            for r in by_variant
        ]

        return {
            "slug": slug,
            "total": total,
            "series": series,
            "variants": variants,
            "referers": [{"name": r["r"], "clicks": r["n"]} for r in referers],
            "recent": [dict(r) for r in recent],
        }

