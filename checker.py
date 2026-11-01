"""Реальная проверка прокси: запрос через прокси к ipify, а не просто открытый порт."""
from __future__ import annotations

import asyncio
import ipaddress
import time

import aiohttp
from aiohttp_socks import ProxyConnector

IP_URL_HTTP = "http://api.ipify.org?format=json"
IP_URL_HTTPS = "https://api.ipify.org?format=json"


async def get_real_ip(http: aiohttp.ClientSession) -> str | None:
    try:
        async with http.get(IP_URL_HTTPS) as r:
            return str((await r.json(content_type=None)).get("ip") or "") or None
    except Exception:  # noqa: BLE001
        return None


async def check_one(ip: str, port: int, proto: str, real_ip: str | None, timeout: float):
    started = time.perf_counter()
    tmo = aiohttp.ClientTimeout(total=timeout)
    try:
        if proto in ("http", "https"):
            # https-прокси проверяем на HTTPS-цели: заодно отсекает прокси, подменяющие сертификаты
            url = IP_URL_HTTPS if proto == "https" else IP_URL_HTTP
            async with aiohttp.ClientSession(timeout=tmo) as s:
                async with s.get(url, proxy=f"http://{ip}:{port}") as r:
                    if r.status != 200:
                        return False, None
                    data = await r.json(content_type=None)
        else:
            conn = ProxyConnector.from_url(f"{proto}://{ip}:{port}", rdns=True)
            async with aiohttp.ClientSession(connector=conn, timeout=tmo) as s:
                async with s.get(IP_URL_HTTP) as r:
                    if r.status != 200:
                        return False, None
                    data = await r.json(content_type=None)
        exit_ip = str(data.get("ip", ""))
        ipaddress.ip_address(exit_ip)
        if real_ip and exit_ip == real_ip:
            return False, None  # прокси ничего не скрывает
        return True, int((time.perf_counter() - started) * 1000)
    except Exception:  # noqa: BLE001
        return False, None


async def check_many(rows, *, concurrency: int, timeout: float, real_ip: str | None):
    """rows: iterable[(id, ip, port, protocol)] -> list[(id, ok, latency_ms)]"""
    sem = asyncio.Semaphore(concurrency)

    async def one(row):
        pid, ip, port, proto = row
        async with sem:
            try:
                ok, lat = await asyncio.wait_for(check_one(ip, port, proto, real_ip, timeout), timeout + 3)
            except Exception:  # noqa: BLE001
                ok, lat = False, None
        return pid, ok, lat

    return list(await asyncio.gather(*(one(r) for r in rows)))
