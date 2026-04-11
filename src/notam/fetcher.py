"""
NOTAM fetchers — one per data source.

All fetchers are async and return a list of raw NOTAM strings
that the parser can then process.

Primary source : notams.online  (free, global, no key required)
Secondary      : CheckWX        (global, free tier with API key)

notams.online API notes
-----------------------
Endpoint : GET /api/notams.php?location=<ICAO>
Response : base64-encoded, then XOR'd with APP_KEY before being JSON-decoded.
           Decoding: base64 → XOR with key → JSON parse → data["notams"][].text
"""

from __future__ import annotations

import asyncio
import base64
from abc import ABC, abstractmethod

import httpx
from loguru import logger


# Key discovered from notams.online/assets/js/app.js
_NOTAMS_ONLINE_KEY = "NotamViewer@1.0.0-OZ_2026!#"


def _xor_decode(data: str, key: str) -> str:
    return "".join(chr(ord(c) ^ ord(key[i % len(key)])) for i, c in enumerate(data))


def _decode_notams_online(encoded: str) -> list[str]:
    """Decode the notams.online XOR+base64 response into a list of raw NOTAM strings."""
    raw = base64.b64decode(encoded).decode("utf-8", errors="replace")
    json_str = _xor_decode(raw, _NOTAMS_ONLINE_KEY)
    import json
    data = json.loads(json_str)
    return [n["text"] for n in data.get("notams", []) if n.get("text")]


class BaseNotamFetcher(ABC):
    """Common interface for all NOTAM sources."""

    source_name: str = "unknown"

    @abstractmethod
    async def fetch(self, icao_codes: list[str]) -> list[str]:
        """Return a list of raw NOTAM strings for the given ICAO codes."""
        ...


# ── notams.online ─────────────────────────────────────────────────────────────

class NotamsOnlineFetcher(BaseNotamFetcher):
    """
    Uses the notams.online free API.
    Global coverage, no API key required.
    Fetches one ICAO at a time (the API is per-airport).
    """

    source_name = "notams.online"
    BASE_URL = "https://notams.online/api/notams.php"

    # Delay between requests to avoid triggering the server's rate limit.
    # notams.online returns 500 when too many concurrent or rapid requests arrive.
    _REQUEST_DELAY_S: float = 0.5

    async def fetch(self, icao_codes: list[str], progress_cb=None) -> list[str]:
        if not icao_codes:
            return []

        raw_notams: list[str] = []
        total = len(icao_codes)

        async with httpx.AsyncClient(timeout=15) as client:
            for i, icao in enumerate(icao_codes):
                if i > 0:
                    await asyncio.sleep(self._REQUEST_DELAY_S)
                try:
                    result = await self._fetch_one(client, icao)
                    raw_notams.extend(result)
                    if result:
                        logger.debug(f"[notams.online] {len(result)} NOTAMs for {icao}")
                except Exception as exc:
                    logger.warning(f"[notams.online] Error for {icao}: {exc}")
                finally:
                    if progress_cb is not None:
                        progress_cb(i + 1, total)

        return raw_notams

    async def _fetch_one(self, client: httpx.AsyncClient, icao: str) -> list[str]:
        resp = await client.get(self.BASE_URL, params={"location": icao})
        if resp.status_code == 500:
            # Retry once after a short back-off — notams.online returns 500 transiently
            await asyncio.sleep(2.0)
            resp = await client.get(self.BASE_URL, params={"location": icao})
        resp.raise_for_status()
        return _decode_notams_online(resp.text.strip())


# ── CheckWX ───────────────────────────────────────────────────────────────────

class CheckWXFetcher(BaseNotamFetcher):
    """
    CheckWX API (checkwx.com) — global NOTAM coverage.
    Requires a free API key.  Returns ICAO-format text.
    """

    source_name = "checkwx"
    BASE_URL = "https://api.checkwx.com/notam"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def fetch(self, icao_codes: list[str], progress_cb=None) -> list[str]:
        if not icao_codes or not self.api_key:
            return []

        raw_notams: list[str] = []
        total = len(icao_codes)
        async with httpx.AsyncClient(
            headers={"X-API-Key": self.api_key}, timeout=15
        ) as client:
            for i, icao in enumerate(icao_codes):
                try:
                    resp = await client.get(f"{self.BASE_URL}/{icao}")
                    resp.raise_for_status()
                    data = resp.json()
                    for item in data.get("data", []):
                        text = item.get("raw", "")
                        if text:
                            raw_notams.append(text)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        logger.debug(f"[checkwx] No NOTAMs for {icao}")
                    else:
                        logger.warning(f"[checkwx] HTTP error for {icao}: {exc}")
                except Exception as exc:
                    logger.error(f"[checkwx] Unexpected error for {icao}: {exc}")
                finally:
                    if progress_cb is not None:
                        progress_cb(i + 1, total)

        return raw_notams


# ── Aggregator ────────────────────────────────────────────────────────────────

class NotamFetcherAggregator:
    """
    Fans out to multiple fetchers and deduplicates results by NOTAM ID.
    """

    def __init__(self, fetchers: list[BaseNotamFetcher]) -> None:
        self.fetchers = fetchers

    async def fetch_all(self, icao_codes: list[str], progress_cb=None) -> list[str]:
        """Fetch from all enabled sources concurrently, deduplicate by ID prefix."""
        tasks = [f.fetch(icao_codes, progress_cb=progress_cb) for f in self.fetchers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        seen: set[str] = set()
        combined: list[str] = []

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Fetcher raised: {result}")
                continue
            for raw in result:
                # Cheap deduplification: first ~12 chars tend to include the NOTAM ID
                key = raw.strip()[:12]
                if key not in seen:
                    seen.add(key)
                    combined.append(raw)

        logger.info(f"Aggregator: {len(combined)} unique raw NOTAMs for {icao_codes}")
        return combined


def build_aggregator(
    notams_online_enabled: bool = True,
    checkwx_api_key: str = "",
) -> NotamFetcherAggregator:
    """Factory that builds an aggregator from config values."""
    fetchers: list[BaseNotamFetcher] = []

    if notams_online_enabled:
        fetchers.append(NotamsOnlineFetcher())

    if checkwx_api_key:
        fetchers.append(CheckWXFetcher(api_key=checkwx_api_key))

    return NotamFetcherAggregator(fetchers)
