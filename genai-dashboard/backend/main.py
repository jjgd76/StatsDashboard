# main.py — FastAPI application for the GenAI Adoption Dashboard
#
# Data modes:
#   MOCK_DATA=true  (or missing API keys) → auto-populated with realistic mock data
#   Real API keys set                     → live data from Anthropic / OpenAI / Microsoft Graph
#
# Upload endpoints are always available to manually inject CSV exports.

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

load_dotenv()

# ── Key Vault (production only) ───────────────────────────────────────────────
_KV_NAME = os.environ.get("KEY_VAULT_NAME")
if _KV_NAME:
    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
        _kv = SecretClient(vault_url=f"https://{_KV_NAME}.vault.azure.net",
                           credential=DefaultAzureCredential())
        for _env, _sec in {
            "ANTHROPIC_ADMIN_API_KEY":        "anthropic-admin-api-key",
            "OPENAI_ADMIN_API_KEY":           "openai-admin-api-key",
            "AZURE_CLIENT_ID":                "azure-client-id",
            "AZURE_CLIENT_SECRET":            "azure-client-secret",
            "AZURE_TENANT_ID":                "azure-tenant-id",
            "AZURE_STORAGE_CONNECTION_STRING":"azure-storage-connection-string",
            "REFRESH_SECRET":                 "refresh-secret",
        }.items():
            if not os.environ.get(_env):
                try:
                    os.environ[_env] = _kv.get_secret(_sec).value  # type: ignore[assignment]
                except Exception:
                    pass
    except Exception as _e:
        logging.getLogger(__name__).warning("Key Vault skipped: %s", _e)

# ── App-level imports (after env is populated) ────────────────────────────────
from parsers import parse_anthropic_csv, parse_copilot_csv, parse_openai_csv
from scheduler import create_scheduler, refresh_all
from storage import MetricsStorage

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%SZ")
logger = logging.getLogger(__name__)

# ── Platform registry ─────────────────────────────────────────────────────────
_UPLOAD_PARSERS = {
    "anthropic":   ("claude",       parse_anthropic_csv),
    "claude":      ("claude",       parse_anthropic_csv),
    "openai":      ("chatgpt",      parse_openai_csv),
    "chatgpt":     ("chatgpt",      parse_openai_csv),
    "copilot":     ("m365_copilot", parse_copilot_csv),
    "m365":        ("m365_copilot", parse_copilot_csv),
    "m365_copilot":("m365_copilot", parse_copilot_csv),
}

# ── Storage + scheduler ───────────────────────────────────────────────────────
_storage: MetricsStorage | None = None
_scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    global _storage, _scheduler
    logger.info("startup: initialising storage")
    try:
        _storage = MetricsStorage()
    except Exception as exc:
        logger.error("startup: storage failed: %s", exc)

    if _storage:
        logger.info("startup: starting scheduler (mock_data=%s)",
                    os.environ.get("MOCK_DATA", "false"))
        _scheduler = create_scheduler(_storage)
        _scheduler.start()
        asyncio.create_task(refresh_all(_storage))  # immediate first run

    yield

    if _scheduler:
        _scheduler.shutdown(wait=False)
    logger.info("shutdown complete")


# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="GenAI Adoption Dashboard API",
    version="3.0.0",
    description="Live or mock metrics for Claude, ChatGPT, and M365 Copilot. "
                "Set MOCK_DATA=true for demo mode.",
    lifespan=lifespan,
)

_cors_origins = [o.strip() for o in
                 os.environ.get("CORS_ORIGINS",
                                "http://localhost:3000,http://localhost:8080,"
                                "http://127.0.0.1:5500,http://localhost:5500").split(",")
                 if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_cors_origins,
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def _log(request: Request, call_next):  # type: ignore[no-untyped-def]
    t = time.monotonic()
    resp: Response = await call_next(request)
    logger.info("http: %s %s → %d (%dms)",
                request.method, request.url.path,
                resp.status_code, int((time.monotonic() - t) * 1000))
    return resp


def _storage_or_503() -> MetricsStorage:
    if _storage is None:
        raise HTTPException(503, "Storage not ready — check server logs")
    return _storage


def _slice_to_period(data: dict[str, Any], period: int) -> dict[str, Any]:
    trend = data.get("daily_trend", [])
    sliced = trend[-period:] if len(trend) >= period else trend
    return {
        **data,
        "period_days": period,
        "total_requests": sum(r.get("requests", 0) for r in sliced),
        "total_cost_usd": round(sum(r.get("cost_usd", 0.0) for r in sliced), 4),
        "daily_trend": sliced,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict[str, Any]:
    mock = os.environ.get("MOCK_DATA", "false").lower() in ("1", "true", "yes")
    return {"status": "ok", "mock_mode": mock,
            "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/metrics/summary")
async def metrics_summary(
    period: int = Query(default=30, description="7, 30, or 90"),
) -> dict[str, Any]:
    if period not in (7, 30, 90):
        raise HTTPException(400, "period must be 7, 30, or 90")
    storage = _storage_or_503()
    snaps = storage.get_all_snapshots(period)
    if not snaps:
        return JSONResponse(status_code=503, content={
            "detail": "No data yet — the background refresh is still running. "
                      "Try again in a few seconds."
        })
    platforms: dict[str, Any] = {}
    last_updated: str | None = None
    for s in snaps:
        platforms[s.get("platform", "unknown")] = s
        fa = s.get("fetched_at")
        if fa and (last_updated is None or fa > last_updated):
            last_updated = fa
    return {"period_days": period, "last_updated": last_updated, "platforms": platforms}


@app.get("/metrics/trend")
async def metrics_trend(
    platform: str = Query(default="claude"),
    days: int    = Query(default=90),
) -> dict[str, Any]:
    valid = {"claude", "chatgpt", "m365_copilot", "all"}
    if platform not in valid:
        raise HTTPException(400, f"platform must be one of {sorted(valid)}")
    storage = _storage_or_503()
    if platform == "all":
        return {"platform": "all", "days": days,
                "trends": {p: storage.get_trend(p, days_back=days)
                           for p in ("claude", "chatgpt", "m365_copilot")}}
    return {"platform": platform, "days": days,
            "trend": storage.get_trend(platform, days_back=days)}


@app.post("/metrics/refresh")
async def metrics_refresh(
    x_refresh_secret: str = Header(default=""),
) -> dict[str, str]:
    secret = os.environ.get("REFRESH_SECRET", "")
    if secret and x_refresh_secret != secret:
        raise HTTPException(403, "Invalid refresh secret")
    storage = _storage_or_503()
    asyncio.create_task(refresh_all(storage))
    return {"status": "refreshing", "triggered_at": datetime.now(timezone.utc).isoformat()}


@app.post("/upload/{platform}")
async def upload_metrics(
    platform: str,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    platform = platform.lower().strip()
    if platform not in _UPLOAD_PARSERS:
        raise HTTPException(400, f"Unknown platform. Valid: anthropic, openai, copilot")
    storage_key, parser_fn = _UPLOAD_PARSERS[platform]
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "File too large (max 10 MB)")
    if not content.strip():
        raise HTTPException(400, "File is empty")
    try:
        metrics = parser_fn(content)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        logger.error("upload error for %s: %s", platform, exc, exc_info=True)
        raise HTTPException(500, "Parse failed — check server logs") from exc

    detected = metrics.get("period_days", 30)
    storage = _storage_or_503()
    stored: list[int] = []
    for bucket in (7, 30, 90):
        if detected >= bucket or bucket == 7:
            storage.save_snapshot(storage_key, bucket, _slice_to_period(metrics, bucket))
            stored.append(bucket)

    return {"status": "ok", "platform": storage_key, "filename": file.filename,
            "detected_period_days": detected, "stored_periods": stored,
            "total_requests": metrics.get("total_requests"),
            "total_cost_usd": metrics.get("total_cost_usd"),
            "uploaded_at": datetime.now(timezone.utc).isoformat()}
