"""Загрузка списков прокси.

Основной источник — открытый API hproxy.com (без ключа). Формат JSON я не мог
проверить вживую, поэтому парсер терпимый: понимает список или словарь со списком
внутри, protocols как список или строку. Если JSON не разобрался — берём txt
отдельно по каждому протоколу.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
from dataclasses import dataclass

import aiohttp

log = logging.getLogger("proxydrop.sources")

HPROXY_API = "https://hproxy.com/api/proxy-list"
VALID = ("http", "https", "socks4", "socks5")
LINE_RE = re.compile(r"^\s*(?:(\w+)://)?(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})\b")


@dataclass(frozen=True)
class Rec:
    ip: str
    port: int
    protocol: str
    country: str | None = None
    anonymity: str | None = None
    latency_ms: int | None = None


def _valid_endpoint(ip: str, port) -> tuple[str, int] | None:
    try:
        addr = ipaddress.ip_address(ip)
        port = int(port)
    except (ValueError, TypeError):
        return None
    # только публичные IPv4 — никаких внутренних адресов
    if addr.version != 4 or not addr.is_global or not (1 <= port <= 65535):
        return None
    return str(addr), port


def _protocols(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = re.split(r"[,/;\s]+", raw)
    out = []
    for p in raw:
        p = str(p).strip().lower()
        if p in VALID and p not in out:
            out.append(p)
    return out


def _find_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("proxies", "data", "results", "items", "list"):
            if isinstance(data.get(key), list):
                return data[key]
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


def parse_json(text: str) -> list[Rec]:
    items = _find_list(json.loads(text))
    out: list[Rec] = []
    for it in items:
        if isinstance(it, str):
            m = LINE_RE.match(it)
            if not m:
                continue
            ep = _valid_endpoint(m.group(2), m.group(3))
            protos = _protocols(m.group(1)) or ["http"]
            if ep:
                out += [Rec(ep[0], ep[1], p) for p in protos]
            continue
        if not isinstance(it, dict):
            continue
        ep = _valid_endpoint(str(it.get("ip") or it.get("host") or ""), it.get("port"))
        if not ep:
            continue
        protos = _protocols(it.get("protocols") or it.get("protocol") or it.get("type")) or ["http"]
        cc = it.get("country_code") or it.get("country")
        cc = cc.upper() if isinstance(cc, str) and len(cc) == 2 else None
        anon = it.get("anonymity")
        anon = anon.lower() if isinstance(anon, str) else None
        lat = it.get("latency_ms")
        lat = int(lat) if isinstance(lat, (int, float)) else None
        out += [Rec(ep[0], ep[1], p, cc, anon, lat) for p in protos]
    return out


def parse_txt(text: str, protocol: str) -> list[Rec]:
    out = []
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        ep = _valid_endpoint(m.group(2), m.group(3))
        if ep:
            out.append(Rec(ep[0], ep[1], _protocols(m.group(1))[0] if _protocols(m.group(1)) else protocol))
    return out


async def _get_text(http: aiohttp.ClientSession, url: str, params=None) -> str:
    async with http.get(url, params=params) as r:
        r.raise_for_status()
        return await r.text()


async def _hproxy_json(http, limit: int) -> list[Rec]:
    text = await _get_text(http, HPROXY_API, {"format": "json", "limit": limit, "recent": "true"})
    recs = parse_json(text)
    if not recs:
        raise ValueError("пустой JSON-ответ")
    return recs


async def _hproxy_txt(http, limit: int) -> list[Rec]:
    out: list[Rec] = []
    for proto in VALID:
        try:
            text = await _get_text(
                http, HPROXY_API, {"format": "txt", "protocol": proto, "limit": limit, "recent": "true"}
            )
            out += parse_txt(text, proto)
        except Exception as e:  # noqa: BLE001
            log.warning("hproxy txt %s: %s", proto, e)
    return out


async def fetch_all(http: aiohttp.ClientSession, limit: int, extra_specs: tuple[str, ...] = ()) -> list[Rec]:
    recs: list[Rec] = []
    try:
        recs = await _hproxy_json(http, limit)
    except Exception as e:  # noqa: BLE001
        log.warning("hproxy json не сработал (%s), пробую txt", e)
        recs = await _hproxy_txt(http, limit)

    for spec in extra_specs:
        proto, _, url = spec.partition("|")
        if not url:
            continue
        try:
            recs += parse_txt(await _get_text(http, url), proto.strip().lower())
        except Exception as e:  # noqa: BLE001
            log.warning("доп. источник %s: %s", url, e)

    seen: set[tuple[str, int, str]] = set()
    uniq: list[Rec] = []
    for r in recs:
        k = (r.ip, r.port, r.protocol)
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq
