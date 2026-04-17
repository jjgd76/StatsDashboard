# anthropic_client.py — Anthropic Admin API client for Claude usage metrics
#
# Required API permissions:
#   ANTHROPIC_ADMIN_API_KEY — Admin key from console.anthropic.com → Settings → API Keys
#   Needs organisation-level usage read access (sk-ant-admin-... prefix)
#
# Mock mode: activated when MOCK_DATA=true OR when ANTHROPIC_ADMIN_API_KEY is not set.

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from mock_data import generate

logger = logging.getLogger(__name__)

_BASE_URL   = "https://api.anthropic.com/v1"
_API_VER    = "2023-06-01"
_MAX_RETRY  = 3
_BASE_DELAY = 2.0


def _is_mock() -> bool:
    return os.environ.get("MOCK_DATA", "").lower() in ("1", "true", "yes") \
        or not os.environ.get("ANTHROPIC_ADMIN_API_KEY", "").strip()


class AnthropicClient:
    def __init__(self) -> None:
        self._api_key = os.environ.get("ANTHROPIC_ADMIN_API_KEY", "")

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key, "anthropic-version": _API_VER}

    async def _get(self, client: httpx.AsyncClient, path: str,
                   params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{_BASE_URL}{path}"
        for attempt in range(1, _MAX_RETRY + 1):
            start = time.monotonic()
            try:
                resp = await client.get(url, headers=self._headers(), params=params)
                ms = int((time.monotonic() - start) * 1000)
                logger.info("anthropic: GET %s → %d (%dms)", url, resp.status_code, ms)

                if resp.status_code == 401:
                    raise PermissionError("Anthropic 401 — check ANTHROPIC_ADMIN_API_KEY")
                if resp.status_code == 429:
                    wait = float(resp.headers.get("retry-after", _BASE_DELAY * attempt))
                    logger.warning("anthropic: 429, retry in %.1fs", wait)
                    await asyncio.sleep(wait)
                    continue
                if resp.status_code >= 500:
                    await asyncio.sleep(_BASE_DELAY ** attempt)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.RequestError as exc:
                logger.error("anthropic: request error: %s", exc)
                if attempt == _MAX_RETRY:
                    raise
                await asyncio.sleep(_BASE_DELAY ** attempt)
        raise RuntimeError(f"anthropic: exhausted retries for {path}")

    async def get_metrics(self, days: int = 30) -> dict[str, Any]:
        if _is_mock():
            logger.info("anthropic: mock mode — returning generated data")
            return generate("claude", days)

        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=days)

        async with httpx.AsyncClient(timeout=30.0) as client:
            usage_res, members_res = await asyncio.gather(
                self._get(client, "/organizations/usage", {
                    "start_date": start.isoformat(),
                    "end_date":   today.isoformat(),
                    "granularity": "day",
                }),
                self._get(client, "/organizations/members"),
                return_exceptions=True,
            )

        total_requests = total_input = total_output = 0
        total_cost: float = 0.0
        model_breakdown: dict[str, dict[str, Any]] = {}
        daily_map: dict[str, dict[str, Any]] = {}

        if isinstance(usage_res, dict):
            for b in usage_res.get("data", []):
                d    = str(b.get("date", ""))[:10]
                m    = b.get("model", "unknown")
                req  = int(b.get("requests", 0))
                inp  = int(b.get("input_tokens", 0))
                out  = int(b.get("output_tokens", 0))
                cost = float(b.get("cost_usd", 0.0))
                total_requests += req; total_input += inp
                total_output += out;   total_cost += cost
                mb = model_breakdown.setdefault(m, {"requests": 0, "cost_usd": 0.0})
                mb["requests"] += req
                mb["cost_usd"]  = round(mb["cost_usd"] + cost, 6)
                if d:
                    row = daily_map.setdefault(d, {"date": d, "requests": 0, "cost_usd": 0.0, "tokens": 0})
                    row["requests"] += req
                    row["cost_usd"]  = round(row["cost_usd"] + cost, 6)
                    row["tokens"]   += inp + out
        elif isinstance(usage_res, Exception):
            logger.error("anthropic: usage error: %s", usage_res)

        active_users: int | None = None
        if isinstance(members_res, dict):
            members = members_res.get("data", members_res.get("members", []))
            if isinstance(members, list):
                active_users = len(members)

        return {
            "platform": "claude",
            "period_days": days,
            "total_requests": total_requests,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cost_usd": round(total_cost, 4),
            "active_users": active_users,
            "model_breakdown": model_breakdown,
            "daily_trend": sorted(daily_map.values(), key=lambda x: x["date"]),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
