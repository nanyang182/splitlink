# -*- coding: utf-8 -*-
"""接口测试：直接用标准库 unittest，不装 pytest。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
from ratelimit import RateLimiter  # noqa: E402


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = app.make_server(db_path=str(Path(cls.tmp.name) / "t.db"), port=0, quiet=True)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.opener = urllib.request.build_opener(NoRedirect)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.server.store.close()
        cls.tmp.cleanup()

    # ---------------------------------------------------------- 工具
    def request(self, method: str, path: str, payload: dict | None = None,
                headers: dict | None = None, follow: bool = True):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        opener = urllib.request.build_opener() if follow else self.opener
        try:
            with opener.open(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, (json.loads(raw) if raw.strip().startswith("{") else raw), resp.headers
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw), e.headers
            except json.JSONDecodeError:
                return e.code, raw, e.headers

    def create(self, **payload):
        return self.request("POST", "/api/links", payload)

    # ---------------------------------------------------------- 用例
    def test_01_health(self):
        status, body, _ = self.request("GET", "/api/health", follow=True)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_02_create_and_redirect(self):
        status, body, headers = self.create(url="https://example.com/a", slug="go-test")
        self.assertEqual(status, 201, body)
        self.assertEqual(body["slug"], "go-test")
        self.assertIn("/go-test", body["short_url"])

        status, _, hdrs = self.request("GET", "/go-test", follow=False)
        self.assertEqual(status, 302)
        self.assertEqual(hdrs["Location"], "https://example.com/a")

    def test_03_invalid_payloads(self):
        cases = [
            {"url": "ftp://example.com"},
            {"url": "http://127.0.0.1/内部地址"},
            {"url": "http://localhost/admin"},
            {"url": ""},
            {"url": "https://example.com", "slug": "a"},          # slug 太短
            {"url": "https://example.com", "slug": "api"},        # 保留字
            {"url": "https://example.com", "variants": []},
            {"url": "https://example.com", "variants": [{"url": "https://a.com", "weight": 0}]},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                status, body, _ = self.create(**payload)
                self.assertEqual(status, 400, body)
                self.assertEqual(body["error"]["code"], "invalid_request")

    def test_04_slug_conflict(self):
        self.create(url="https://example.com/x", slug="dup-slug")
        status, body, _ = self.create(url="https://example.com/y", slug="dup-slug")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "slug_taken")

    def test_05_stats_counts(self):
        self.create(url="https://example.com/s", slug="stat-slug")
        for _ in range(3):
            self.request("GET", "/stat-slug", follow=False)
        status, body, _ = self.request("GET", "/api/links/stat-slug/stats")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 3)
        self.assertEqual(len(body["series"]), 14)
        self.assertEqual(sum(v["clicks"] for v in body["variants"]), 3)

    def test_06_split_variants(self):
        status, body, _ = self.create(
            url="https://example.com/a",
            slug="split-slug",
            variants=[
                {"url": "https://example.com/a", "weight": 1, "label": "A"},
                {"url": "https://example.com/b", "weight": 1, "label": "B"},
            ],
        )
        self.assertEqual(status, 201, body)
        targets = set()
        for _ in range(30):
            status, _, hdrs = self.request("GET", "/split-slug", follow=False)
            self.assertEqual(status, 302)
            targets.add(hdrs["Location"])
        # 权重相同时两个目标都应被命中（30 次全落一边的概率约 2^-29）
        self.assertEqual(targets, {"https://example.com/a", "https://example.com/b"})

    def test_07_expired_link(self):
        self.create(url="https://example.com/e", slug="exp-slug", expires_in_days=1)
        link = self.server.store.get("exp-slug")
        # 直接把过期时间改到过去，避免测试等一天
        with self.server.store._lock:  # noqa: SLF001
            self.server.store._conn.execute(
                "UPDATE links SET expires_at = '2000-01-01T00:00:00Z' WHERE slug = ?", ("exp-slug",))
            self.server.store._conn.commit()
        status, _, _ = self.request("GET", "/exp-slug", follow=False)
        self.assertEqual(status, 410)

    def test_08_delete(self):
        self.create(url="https://example.com/d", slug="del-slug")
        status, _, _ = self.request("DELETE", "/api/links/del-slug")
        self.assertEqual(status, 204)
        status, _, _ = self.request("GET", "/api/links/del-slug")
        self.assertEqual(status, 404)

    def test_09_not_found(self):
        status, body, _ = self.request("GET", "/no-such-slug", follow=False)
        self.assertEqual(status, 404)
        status, _, _ = self.request("GET", "/api/links/no-such-slug")
        self.assertEqual(status, 404)

    def test_10_rate_limit(self):
        # 换一个新的限流器，避免受前面用例影响
        app.LIMIT_WRITE = RateLimiter(rate=3, per_seconds=60)
        try:
            codes = [self.create(url=f"https://example.com/r{i}")[0] for i in range(5)]
        finally:
            app.LIMIT_WRITE = RateLimiter(rate=30, per_seconds=60)
        self.assertIn(201, codes)
        self.assertEqual(codes[-1], 429)


if __name__ == "__main__":
    unittest.main(verbosity=2)

