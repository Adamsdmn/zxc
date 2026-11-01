"""Crypto Pay API (@CryptoBot). Документация: https://help.crypt.bot/crypto-pay-api"""
from __future__ import annotations

import aiohttp


class CryptoPayError(Exception):
    pass


class CryptoPay:
    def __init__(self, token: str, testnet: bool = False):
        self.token = token
        self.base = "https://testnet-pay.crypt.bot/api" if testnet else "https://pay.crypt.bot/api"
        self._session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _call(self, method: str, http_method: str = "POST", **params):
        s = await self._sess()
        headers = {"Crypto-Pay-API-Token": self.token}
        url = f"{self.base}/{method}"
        if http_method == "POST":
            resp = await s.post(url, json=params, headers=headers)
        else:
            resp = await s.get(url, params=params, headers=headers)
        async with resp:
            data = await resp.json(content_type=None)
        if not data.get("ok"):
            raise CryptoPayError(str(data.get("error")))
        return data["result"]

    async def create_invoice(self, asset: str, amount: str, description: str, payload: str, expires_in: int = 3600) -> dict:
        return await self._call(
            "createInvoice",
            asset=asset, amount=amount, description=description[:900],
            payload=payload, expires_in=expires_in, allow_comments=False,
        )

    async def get_invoice(self, invoice_id: int) -> dict | None:
        result = await self._call("getInvoices", "GET", invoice_ids=str(invoice_id))
        items = result.get("items", []) if isinstance(result, dict) else result
        return items[0] if items else None
