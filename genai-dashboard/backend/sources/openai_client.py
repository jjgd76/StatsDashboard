# openai_client.py — OpenAI Organisation Admin API client for ChatGPT usage metrics
#
# Required API permissions:
#   OPENAI_ADMIN_API_KEY — Admin key from platform.openai.com → Settings → API Keys
#   Needs org:read and billing:read permissions (owner role)
#
# Mock mode: activated when MOCK_DATA=true OR when OPENAI_ADMIN_API_KEY is not set.

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from mock_data import generate

logger = logging.getLogger(__name__)

_BASE_URL   = "https://api.openai.com/v1"
_MAX_RETRY  = 3
_BASE_DELAY = 2.0


def _is_mock() -> bool:
    return os.environ.get("MOCK_DATA", "").lower() in ("1", "true", "yes") \
        or not os.environ.get("OPENAI_ADMIN_API_KEY", "").strip()


class OpenAIClient:
    def __init__(self) -> None:
        self._api_key = os.environ.get("OPENAI_ADMIN_API_KEY", "")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _ts(self, dt: datetime) -> int:
        return int(dt.timestamp())

    async def _get(self, client: httpx.AsyncClient, path: str,
                   params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{_BASE_URL}{path}"
        for attempt in range(1, _MAX_RETRY + 1):
            start = time.monotonic()
            try:
                resp = await client.get(url, headers=self._headers(), params=params)
                ms = int((time.monotonic() - start) * 1000)
                logger.info("openai: GET %s → %d (%dms)", url, resp.status_code, ms)
                if resp.status_code == 401:
                    raise PermissionError("OpenAI 401 — check OPENAI_ADMIN_API_KEY")
                if resp.status_code == 429:
                    wait = float(resp.headers.get("retry-after", _BASE_DELAY * attempt))
                    await asyncio.sleep(wait)
                    continue
                if resp.status_code >= 500:
                    await asyncio.sleep(_BASE_DELAY ** attempt)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.RequestError as exc:
                logger.error("openai: request error: %s", exc)
                if attempt == _MAX_RETRY:
                    raise
                await asyncio.sleep(_BASE_DELAY ** attempt)
        raise RuntimeError(f"openai: exhausted retries for {path}")

    async def get_metrics(self, days: int = 30) -> dict[str, Any]:
        if _is_mock():
            logger.info("openai: mock mode — returning generated data")
            return generate("chatgpt", days)

        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        start = today - timedelta(days=days)

        async with httpx.AsyncClient(timeout=30.0) as client:
            usage_res, costs_res, users_res = await asyncio.gather(
                self._get(client, "/organization/usage", {
                    "start_time": self._ts(start), "end_time": self._ts(today), "bucket_width": "1d",
                }),
                self._get(client, "/organization/costs", {
                    "start_time": self._ts(start), "end_time": self._ts(today), "bucket_width": "1d",
                }),
                self._get(client, "/organization/users"),
                return_exceptions=True,
            )

        cost_by_date: dict[str, float] = {}
        if isinstance(costs_res, dict):
            for b in costs_res.get("data", []):
                ts = b.get("start_time", 0)
                d  = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
                cost_by_date[d] = cost_by_date.get(d, 0.0) + float(b.get("amount", {}).get("value", 0.0))

        total_requests = total_input = total_output = 0
        model_breakdown: dict[str, dict[str, Any]] = {}
        daily_map: dict[str, dict[str, Any]] = {}

        if isinstance(usage_res, dict):
            for b in usage_res.get("data", []):
                ts  = b.get("start_time", 0)
                d   = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
                m   = b.get("model", "unknown")
                req = int(b.get("num_requests", b.get("requests", 0)))
                inp = int(b.get("input_tokens", b.get("n_context_tokens_total", 0)))
                out = int(b.get("output_tokens", b.get("n_generated_tokens_total", 0)))
                total_requests += req; total_input += inp; total_output += out
                mb = model_breakdown.setdefault(m, {"requests": 0, "cost_usd": 0.0})
                mb["requests"] += req
                row = daily_map.setdefault(d, {"date": d, "requests": 0, "cost_usd": 0.0, "tokens": 0})
                row["requests"] += req
                row["tokens"]   += inp + out
        elif isinstance(usage_res, Exception):
            logger.error("openai: usage error: %s", usage_res)

        total_cost = sum(cost_by_date.values())
        for d, cost in cost_by_date.items():
            if d in daily_map:
                daily_map[d]["cost_usd"] = round(cost, 6)
        total_req = max(total_requests, 1)
        for mb in model_breakdown.values():
            mb["cost_usd"] = round(total_cost * mb["requests"] / total_req, 6)

        active_users: int | None = None
        if isinstance(users_res, dict):
            u = users_res.get("data", users_res.get("users", []))
            if isinstance(u, list):
                active_users = len(u)

        return {
            "platform": "chatgpt",
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
