# -*- coding: utf-8 -*-
"""链接校验：只接受 http/https，拒绝本机与内网地址（避免把服务变成 SSRF 跳板）。"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
RESERVED_SLUGS = {
    "api", "ui", "static", "health", "admin", "login", "logout",
    "favicon.ico", "robots.txt", "docs",
}
MAX_URL_LEN = 2048


class ValidationError(ValueError):
    """入参不合法，映射为 HTTP 400。"""


def normalize_url(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError("url 不能为空")
    url = raw.strip()
    if len(url) > MAX_URL_LEN:
        raise ValidationError(f"url 不能超过 {MAX_URL_LEN} 字符")

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValidationError("只支持 http / https 链接")
    if not parsed.hostname:
        raise ValidationError("url 缺少域名")

    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"}:
        raise ValidationError("不允许指向本机地址")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local):
        raise ValidationError("不允许指向内网地址")

    return url


def normalize_slug(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError("slug 不能为空")
    slug = raw.strip()
    if not SLUG_RE.match(slug):
        raise ValidationError("slug 只能是 3-32 位的字母、数字、下划线或短横线")
    if slug.lower() in RESERVED_SLUGS:
        raise ValidationError("slug 与系统路径冲突，请换一个")
    return slug


def normalize_variants(payload: dict) -> list[dict]:
    """variants 可选；不传就把 url 当成唯一目标。"""
    raw = payload.get("variants")
    if raw is None:
        return [{"url": normalize_url(payload.get("url")), "weight": 1, "label": "默认"}]

    if not isinstance(raw, list) or not raw:
        raise ValidationError("variants 必须是非空数组")
    if len(raw) > 10:
        raise ValidationError("variants 最多 10 个")

    out = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValidationError(f"variants[{i}] 必须是对象")
        weight = item.get("weight", 1)
        if not isinstance(weight, int) or isinstance(weight, bool) or not (1 <= weight <= 100):
            raise ValidationError(f"variants[{i}].weight 必须是 1-100 的整数")
        label = item.get("label") or chr(65 + i)  # A, B, C...
        if not isinstance(label, str) or len(label) > 20:
            raise ValidationError(f"variants[{i}].label 过长")
        out.append({"url": normalize_url(item.get("url")), "weight": weight, "label": label})
    return out


def normalize_expires(raw: object) -> int | None:
    """expires_in_days：0 或省略表示永不过期，返回天数。"""
    if raw in (None, "", 0, "0"):
        return None
    if not isinstance(raw, int) or isinstance(raw, bool) or not (1 <= raw <= 3650):
        raise ValidationError("expires_in_days 必须是 1-3650 的整数")
    return raw
