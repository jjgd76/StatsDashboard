# graph_client.py — Microsoft Graph API client for M365 Copilot usage metrics
#
# Required Azure AD app permissions (application, not delegated):
#   Reports.Read.All  — to call getMicrosoft365CopilotUsageSummary
#   ReportSettings.Read.All  — if report anonymisation is enabled in tenant
#
# Setup: Register an app in Entra ID, grant admin consent for Reports.Read.All,
#        create a client secret, populate AZURE_TENANT_ID, AZURE_CLIENT_ID,
#        AZURE_CLIENT_SECRET in env.
#
# Mock mode: activated when MOCK_DATA=true OR when AZURE_TENANT_ID is not set.

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from mock_data import generate

logger = logging.getLogger(__name__)

_TOKEN_URL  = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_SCOPE      = "https://graph.microsoft.com/.default"
_MAX_RETRY  = 3
_BASE_DELAY = 2.0


def _is_mock() -> bool:
    return os.environ.get("MOCK_DATA", "").lower() in ("1", "true", "yes") \
        or not os.environ.get("AZURE_TENANT_ID", "").strip()


class GraphClient:
    def __init__(self) -> None:
        self._tenant    = os.environ.get("AZURE_TENANT_ID", "")
        self._client_id = os.environ.get("AZURE_CLIENT_ID", "")
        self._secret    = os.environ.get("AZURE_CLIENT_SECRET", "")
        self._token: str | None = None
        self._token_expiry: float = 0.0

    async def _get_token(self, client: httpx.AsyncClient) -> str:
        if self._token and time.monotonic() < self._token_expiry - 30:
            return self._token
        resp = await client.post(
            _TOKEN_URL.format(tenant=self._tenant),
            data={"grant_type": "client_credentials", "client_id": self._client_id,
                  "client_secret": self._secret, "scope": _SCOPE},
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expiry = time.monotonic() + payload.get("expires_in", 3600)
        return self._token  # type: ignore[return-value]

    async def _get(self, client: httpx.AsyncClient, url: str, token: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}"}
        for attempt in range(1, _MAX_RETRY + 1):
            start = time.monotonic()
            try:
                resp = await client.get(url, headers=headers)
                ms = int((time.monotonic() - start) * 1000)
                logger.info("graph: GET %s → %d (%dms)", url, resp.status_code, ms)
                if resp.status_code == 401:
                    self._token = None
                    token = await self._get_token(client)
                    headers = {"Authorization": f"Bearer {token}"}
                    continue
                if resp.status_code == 429:
                    await asyncio.sleep(float(resp.headers.get("Retry-After", _BASE_DELAY * attempt)))
                    continue
                if resp.status_code == 503:
                    await asyncio.sleep(_BASE_DELAY ** attempt)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.RequestError as exc:
                logger.error("graph: request error: %s", exc)
                if attempt == _MAX_RETRY:
                    raise
                await asyncio.sleep(_BASE_DELAY ** attempt)
        raise RuntimeError(f"graph: exhausted retries for {url}")

    async def get_metrics(self, days: int = 30) -> dict[str, Any]:
        if _is_mock():
            logger.info("graph: mock mode — returning generated data")
            return generate("m365_copilot", days)

        period = f"D{days}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            token = await self._get_token(client)
            usage_res, seat_res = await asyncio.gather(
                self._get(client, f"{_GRAPH_BASE}/reports/getMicrosoft365CopilotUsageSummary(period='{period}')", token),
                self._get(client, f"{_GRAPH_BASE}/reports/getMicrosoft365CopilotUserCountSummary(period='{period}')", token),
                return_exceptions=True,
            )

        active_users = 0
        app_breakdown = {k: 0 for k in ("teams", "word", "outlook", "excel", "powerpoint")}

        if isinstance(usage_res, dict):
            for entry in usage_res.get("value", [usage_res]):
                active_users = max(active_users, entry.get("activeUsers", 0))
                for app in app_breakdown:
                    for key in (app, app.capitalize(), f"{app}ActiveUsers"):
                        if key in entry:
                            app_breakdown[app] = max(app_breakdown[app], entry[key])
        elif isinstance(usage_res, Exception):
            logger.error("graph: usage error: %s", usage_res)

        licensed_seats = 0
        if isinstance(seat_res, dict):
            for entry in seat_res.get("value", [seat_res]):
                licensed_seats = max(licensed_seats, entry.get("licensedCopilotUsers", 0))
                if not active_users:
                    active_users = entry.get("activeCopilotUsers", 0)
        elif isinstance(seat_res, Exception):
            logger.error("graph: seat error: %s", seat_res)

        return {
            "platform": "m365_copilot",
            "period_days": days,
            "active_users": active_users,
            "licensed_seats": licensed_seats,
            "adoption_rate_pct": round((active_users / licensed_seats) * 100, 2) if licensed_seats else 0.0,
            "app_breakdown": app_breakdown,
            "total_requests": None,
            "total_cost_usd": 0.0,
            "daily_trend": [],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
