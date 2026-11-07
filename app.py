# -*- coding: utf-8 -*-
"""
SplitLink · 带 A/B 分流的短链服务

只用 Python 标准库：http.server + sqlite3，没有任何第三方依赖。
运行：python app.py           （默认 127.0.0.1:8787）

接口：
    GET    /                        控制台（静态页）
    GET    /api/health              健康检查
    POST   /api/links               创建短链（可带多目标分流）
    GET    /api/links               最近创建的短链
    GET    /api/links/<slug>        短链详情
    GET    /api/links/<slug>/stats  点击统计
    DELETE /api/links/<slug>        删除
    GET    /<slug>                  302 跳转（按权重选目标，并记一次点击）
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import string
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from ratelimit import RateLimiter
from store import Store
from validate import (ValidationError, normalize_expires, normalize_slug,
                      normalize_variants)

ROOT = Path(__file__).resolve().parent
DB_PATH = os.environ.get("SPLITLINK_DB", str(ROOT / "data" / "splitlink.db"))
STATIC = ROOT / "static"
HOST = os.environ.get("SPLITLINK_HOST", "127.0.0.1")
PORT = int(os.environ.get("SPLITLINK_PORT", "8787"))

ALPHABET = string.ascii_letters + string.digits
SLUG_LEN = 7

# 写操作 30 次/分钟；跳转 300 次/分钟（单 IP）
LIMIT_WRITE = RateLimiter(rate=30, per_seconds=60)
LIMIT_READ = RateLimiter(rate=300, per_seconds=60)

_INDEX_HTML = ""


def gen_slug() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(SLUG_LEN))


def mask_ip(ip: str) -> str:
    """只存哈希，不留原始 IP——日志里也不该出现可回溯的个人信息。"""
    return hashlib.sha256(("splitlink:" + ip).encode()).hexdigest()[:16]


class Handler(BaseHTTPRequestHandler):
    server_version = "SplitLink/1.0"
    protocol_version = "HTTP/1.1"

    store: Store
    token: str | None
    started_at: float
    quiet: bool = False

    # ------------------------------------------------------------ 基础
    def log_message(self, fmt: str, *args) -> None:  # 结构化日志，一行一条
        if self.quiet:
            return
        sys.stderr.write(json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "ip": mask_ip(self.client_address[0]),
            "method": self.command,
            "path": self.path,
            "msg": fmt % args,
        }, ensure_ascii=False) + "\n")

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: dict, extra: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"}
        headers.update(extra or {})
        self._send(code, body, "application/json; charset=utf-8", headers)

    def _error(self, code: int, err: str, message: str, extra: dict | None = None) -> None:
        self._json(code, {"error": {"code": err, "message": message}}, extra)

    def _body_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValidationError("Content-Length 不合法")
        if length > 64 * 1024:
            raise ValidationError("请求体过大")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValidationError("请求体必须是合法 JSON")
        if not isinstance(data, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return data

    def _authorized(self) -> bool:
        if not self.token:
            return True  # 未设置 token 时是本地开发模式
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {self.token}"

    # ------------------------------------------------------------ 路由
    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, b"", "text/plain", {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Max-Age": "600",
        })

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        try:
            path = unquote(urlparse(self.path).path)
            ip = self.client_address[0]

            if path in ("/", "/index.html"):
                return self._serve_index()
            if path == "/api/health":
                return self._json(200, {
                    "status": "ok",
                    "links": len(self.store.list_links(limit=10000)),
                    "uptime_seconds": int(time.time() - self.started_at),
                    "auth": "token" if self.token else "open",
                })
            if path == "/api/links":
                return self._json(200, {"items": self.store.list_links()})

            m = re.fullmatch(r"/api/links/([^/]+)/stats", path)
            if m:
                stats = self.store.stats(m.group(1))
                if stats is None:
                    return self._error(404, "not_found", "短链不存在")
                return self._json(200, stats)

            m = re.fullmatch(r"/api/links/([^/]+)", path)
            if m:
                link = self.store.get(m.group(1))
                if link is None:
                    return self._error(404, "not_found", "短链不存在")
                return self._json(200, link)

            # 其余一律当作短链跳转
            slug = path.strip("/")
            if not slug or "/" in slug:
                return self._error(404, "not_found", "路径不存在")
            if not LIMIT_READ.allow(ip):
                return self._error(429, "rate_limited", "请求过于频繁",
                                   {"Retry-After": str(LIMIT_READ.retry_after(ip))})

            link = self.store.get(slug)
            if link is None:
                return self._html(404, f"<h1>404</h1><p>短链 <code>{slug}</code> 不存在。</p>")
            if self.store.is_expired(link):
                return self._html(410, f"<h1>410</h1><p>短链 <code>{slug}</code> 已过期。</p>")

            variant = self.store.pick_variant(link)
            if self.command != "HEAD":
                self.store.record_click(
                    slug, variant["id"],
                    self.headers.get("Referer"),
                    self.headers.get("User-Agent"),
                    mask_ip(ip),
                )
            return self._send(302, b"", "text/plain", {
                "Location": variant["target"],
                "Cache-Control": "no-store",
            })
        except Exception:
            traceback.print_exc()
            return self._error(500, "internal", "服务器内部错误")

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = unquote(urlparse(self.path).path)
            if path != "/api/links":
                return self._error(404, "not_found", "路径不存在")
            if not self._authorized():
                return self._error(401, "unauthorized", "缺少或错误的 Authorization")
            ip = self.client_address[0]
            if not LIMIT_WRITE.allow(ip):
                return self._error(429, "rate_limited", "创建太频繁，稍后再试",
                                   {"Retry-After": str(LIMIT_WRITE.retry_after(ip))})

            payload = self._body_json()
            variants = normalize_variants(payload)
            expires = normalize_expires(payload.get("expires_in_days"))

            slug = payload.get("slug")
            slug = normalize_slug(slug) if slug else gen_slug()
            if self.store.exists(slug):
                if payload.get("slug"):
                    return self._error(409, "slug_taken", "这个 slug 已经被占用了")
                for _ in range(5):  # 自动生成的撞了，重试
                    slug = gen_slug()
                    if not self.store.exists(slug):
                        break
                else:
                    return self._error(500, "slug_exhausted", "生成 slug 失败，请重试")

            link = self.store.create(slug, variants, expires, owner=payload.get("owner"))
            host = self.headers.get("Host") or f"{HOST}:{PORT}"
            return self._json(201, {
                "slug": slug,
                "short_url": f"http://{host}/{slug}",
                "expires_at": link["expires_at"],
                "variants": [{"label": v["label"], "target": v["target"], "weight": v["weight"]}
                             for v in link["variants"]],
            }, {"Location": f"http://{host}/{slug}"})
        except ValidationError as e:
            return self._error(400, "invalid_request", str(e))
        except Exception:
            traceback.print_exc()
            return self._error(500, "internal", "服务器内部错误")

    def do_DELETE(self) -> None:  # noqa: N802
        try:
            path = unquote(urlparse(self.path).path)
            m = re.fullmatch(r"/api/links/([^/]+)", path)
            if not m:
                return self._error(404, "not_found", "路径不存在")
            if not self._authorized():
                return self._error(401, "unauthorized", "缺少或错误的 Authorization")
            if not self.store.delete(m.group(1)):
                return self._error(404, "not_found", "短链不存在")
            return self._send(204, b"", "text/plain", {"Access-Control-Allow-Origin": "*"})
        except Exception:
            traceback.print_exc()
            return self._error(500, "internal", "服务器内部错误")

    # ------------------------------------------------------------ 静态
    def _serve_index(self) -> None:
        global _INDEX_HTML
        if not _INDEX_HTML:
            _INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
        body = _INDEX_HTML.encode("utf-8")
        self._send(200, body, "text/html; charset=utf-8", {"Cache-Control": "no-cache"})

    def _html(self, code: int, inner: str) -> None:
        body = (
            "<!doctype html><meta charset='utf-8'>"
            "<title>SplitLink</title>"
            "<style>body{font-family:system-ui,'Microsoft YaHei',sans-serif;"
            "background:#0A1B2E;color:#EAF2FB;display:grid;place-items:center;height:100vh;margin:0}"
            "code{background:#16293F;padding:2px 6px;border-radius:4px}</style>"
            f"<div>{inner}</div>"
        ).encode("utf-8")
        self._send(code, body, "text/html; charset=utf-8")


def make_server(db_path: str | None = None, token: str | None = None,
                host: str = HOST, port: int = PORT, quiet: bool = False) -> ThreadingHTTPServer:
    """工厂函数：测试里可以传端口 0 和临时数据库。"""
    store = Store(db_path or DB_PATH)
    handler = type("BoundHandler", (Handler,), {
        "store": store,
        "token": token,
        "started_at": time.time(),
        "quiet": quiet,
    })
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.store = store  # type: ignore[attr-defined]
    return server


def main() -> None:
    token = os.environ.get("SPLITLINK_TOKEN") or None
    server = make_server(token=token)
    port = server.server_address[1]
    mode = "token 鉴权" if token else "本地开发模式（未设置 SPLITLINK_TOKEN，写接口不校验）"
    print(f"SplitLink 已启动: http://{HOST}:{port}/    [{mode}]")
    print(f"数据库: {DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
        server.store.close()  # type: ignore[attr-defined]


if __name__ == "__main__":
    main()

