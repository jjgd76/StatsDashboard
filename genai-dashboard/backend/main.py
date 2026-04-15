# main.py — FastAPI application for the GenAI Adoption Dashboard (upload-based)
#
# Data flow:
#   1. User exports CSV stats from Anthropic Console / OpenAI Platform / M365 Admin Center
#   2. User uploads the file via POST /upload/{platform}
#   3. Backend parses → normalises → stores in Azure Table Storage
#   4. Frontend fetches /metrics/summary and /metrics/trend to render the dashboard

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, Response, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import time

# Load .env for local development (no-op if env vars already set)
load_dotenv()

# ---------------------------------------------------------------------------
# Key Vault secret loading (production — skipped gracefully if not configured)
# ---------------------------------------------------------------------------

_KV_NAME = os.environ.get("KEY_VAULT_NAME")
if _KV_NAME:
    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient

        _kv_url = f"https://{_KV_NAME}.vault.azure.net"
        _kv_client = SecretClient(vault_url=_kv_url, credential=DefaultAzureCredential())
        _SECRET_MAP = {
            "AZURE_STORAGE_CONNECTION_STRING": "azure-storage-connection-string",
        }
        for env_key, secret_name in _SECRET_MAP.items():
            if not os.environ.get(env_key):
                try:
                    os.environ[env_key] = _kv_client.get_secret(secret_name).value  # type: ignore[assignment]
                except Exception:
                    pass
    except Exception as kv_exc:
        logging.getLogger(__name__).warning("Key Vault load skipped: %s", kv_exc)

# ---------------------------------------------------------------------------
# Imports that depend on env vars being populated
# ---------------------------------------------------------------------------

from parsers import parse_anthropic_csv, parse_openai_csv, parse_copilot_csv
from storage import MetricsStorage

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform registry
# ---------------------------------------------------------------------------

_PLATFORM_PARSERS = {
    "anthropic": ("claude",      parse_anthropic_csv),
    "claude":    ("claude",      parse_anthropic_csv),
    "openai":    ("chatgpt",     parse_openai_csv),
    "chatgpt":   ("chatgpt",     parse_openai_csv),
    "copilot":   ("m365_copilot", parse_copilot_csv),
    "m365":      ("m365_copilot", parse_copilot_csv),
    "m365_copilot": ("m365_copilot", parse_copilot_csv),
}

_VALID_PLATFORMS = {"anthropic", "openai", "copilot"}  # canonical names shown in docs

# ---------------------------------------------------------------------------
# Storage (initialised at startup)
# ---------------------------------------------------------------------------

_storage: MetricsStorage | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    global _storage
    logger.info("main: initialising storage")
    try:
        _storage = MetricsStorage()
    except Exception as exc:
        logger.error("main: storage initialisation failed: %s", exc)
        # Continue anyway — endpoints will return 503 until storage is available
    yield
    logger.info("main: shutting down")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="GenAI Adoption Dashboard API",
    description=(
        "Upload CSV/JSON exports from Anthropic Console, OpenAI Platform, and "
        "Microsoft 365 Admin Center to populate the dashboard."
    ),
    version="2.0.0",
    lifespan=lifespan,
)

_cors_origins_raw = os.environ.get(
    "CORS_ORIGINS", "http://localhost:3000,http://localhost:8080,http://127.0.0.1:5500"
)
_cors_origins = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
    start = time.monotonic()
    response: Response = await call_next(request)
    duration_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "http: %s %s → %d (%dms)",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_storage() -> MetricsStorage:
    if _storage is None:
        raise HTTPException(status_code=503, detail="Storage not initialised. Check server logs.")
    return _storage


def _bucket_period(period_days: int) -> int:
    """Round a detected period to the nearest supported bucket (7, 30, 90)."""
    if period_days <= 10:
        return 7
    if period_days <= 45:
        return 30
    return 90


def _slice_to_period(data: dict[str, Any], period: int) -> dict[str, Any]:
    """
    Return a copy of the metrics dict with daily_trend trimmed to the last `period` days.
    Also recomputes aggregates from the sliced trend so totals match the requested window.
    """
    trend: list[dict[str, Any]] = data.get("daily_trend", [])
    if not trend:
        return {**data, "period_days": period}

    sliced = trend[-period:] if len(trend) >= period else trend

    total_requests = sum(r.get("requests", 0) for r in sliced)
    total_cost = round(sum(r.get("cost_usd", 0.0) for r in sliced), 4)
    total_tokens = sum(r.get("tokens", 0) for r in sliced)

    return {
        **data,
        "period_days": period,
        "total_requests": total_requests,
        "total_cost_usd": total_cost,
        "total_input_tokens": total_tokens // 2,   # approximation when not stored separately
        "total_output_tokens": total_tokens // 2,
        "daily_trend": sliced,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.post("/upload/{platform}")
async def upload_metrics(
    platform: str,
    file: UploadFile = File(..., description="CSV or JSON export from the platform's admin console"),
) -> dict[str, Any]:
    """
    Upload a usage export file for a platform.

    **platform** — one of: `anthropic`, `openai`, `copilot`

    The file should be the CSV (or JSON) exported directly from the platform's
    admin console. See README for exact export steps per platform.
    """
    platform = platform.lower().strip()
    if platform not in _PLATFORM_PARSERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown platform '{platform}'. "
                f"Valid values: {', '.join(sorted(_VALID_PLATFORMS))}"
            ),
        )

    storage_key, parser_fn = _PLATFORM_PARSERS[platform]

    # Read uploaded bytes (limit 10 MB)
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")
    if not content.strip():
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    # Parse
    try:
        metrics = parser_fn(content)
    except ValueError as exc:
        logger.warning("upload: parse error for %s: %s", platform, exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("upload: unexpected error for %s: %s", platform, exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to parse file. Check server logs.") from exc

    detected_period = metrics.get("period_days", 30)
    logger.info(
        "upload: parsed %s — detected period %d days, %d requests",
        storage_key,
        detected_period,
        metrics.get("total_requests", 0),
    )

    # Store under all applicable period buckets so the dashboard can serve any window
    storage = _require_storage()
    stored_periods: list[int] = []
    for bucket in (7, 30, 90):
        # Only store a bucket if the data covers at least that many days,
        # or if it's the smallest bucket (always store at least 7d)
        if detected_period >= bucket or bucket == 7:
            sliced = _slice_to_period(metrics, bucket)
            storage.save_snapshot(storage_key, bucket, sliced)
            stored_periods.append(bucket)

    return {
        "status": "ok",
        "platform": storage_key,
        "filename": file.filename,
        "detected_period_days": detected_period,
        "stored_periods": stored_periods,
        "total_requests": metrics.get("total_requests"),
        "total_cost_usd": metrics.get("total_cost_usd"),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/metrics/summary")
async def metrics_summary(
    period: int = Query(default=30, description="Period in days: 7, 30, or 90"),
) -> dict[str, Any]:
    """Return the latest snapshot for all three platforms side-by-side."""
    if period not in (7, 30, 90):
        raise HTTPException(status_code=400, detail="period must be 7, 30, or 90")

    storage = _require_storage()
    snapshots = storage.get_all_snapshots(period)

    if not snapshots:
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "No metrics data available yet. "
                    "Upload a CSV export for at least one platform to get started."
                )
            },
        )

    platforms: dict[str, Any] = {}
    last_updated: str | None = None
    for snap in snapshots:
        name = snap.get("platform", "unknown")
        platforms[name] = snap
        fa = snap.get("fetched_at")
        if fa and (last_updated is None or fa > last_updated):
            last_updated = fa

    return {
        "period_days": period,
        "last_updated": last_updated,
        "platforms": platforms,
    }


@app.get("/metrics/trend")
async def metrics_trend(
    platform: str = Query(default="claude", description="Platform: claude | chatgpt | m365_copilot | all"),
    days: int = Query(default=90, description="Number of days of history to return"),
) -> dict[str, Any]:
    """Return the daily trend series for charting."""
    valid = {"claude", "chatgpt", "m365_copilot", "all"}
    if platform not in valid:
        raise HTTPException(status_code=400, detail=f"platform must be one of {sorted(valid)}")

    storage = _require_storage()

    if platform == "all":
        trends: dict[str, list[dict[str, Any]]] = {}
        for p in ("claude", "chatgpt", "m365_copilot"):
            trends[p] = storage.get_trend(p, days_back=days)
        return {"platform": "all", "days": days, "trends": trends}

    return {
        "platform": platform,
        "days": days,
        "trend": storage.get_trend(platform, days_back=days),
    }


@app.get("/uploads/history")
async def upload_history() -> dict[str, Any]:
    """Return a list of the most recent uploads for each platform."""
    storage = _require_storage()
    history: dict[str, Any] = {}
    for platform in ("claude", "chatgpt", "m365_copilot"):
        recent = storage.get_trend(platform, days_back=10)
        if recent:
            last = recent[-1]
            history[platform] = {
                "last_upload": last.get("fetched_at"),
                "available": True,
            }
        else:
            history[platform] = {"last_upload": None, "available": False}
    return {"history": history}
