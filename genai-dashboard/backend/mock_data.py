# mock_data.py — Generates realistic mock metrics for all three GenAI platforms.
#
# Used automatically when MOCK_DATA=true OR when an API key is missing.
# Uses a fixed random seed so the data is deterministic across restarts.

import random
from datetime import date, datetime, timedelta, timezone
from typing import Any


def _daily_series(base: float, days: int, growth_total: float = 0.3,
                  noise: float = 0.12, seed: int = 42) -> list[float]:
    """Return `days` daily values with realistic growth, noise, and weekend dips."""
    rng = random.Random(seed)
    today = date.today()
    value = base / (1 + growth_total)  # start lower, grow to base
    values: list[float] = []
    for i in range(days):
        day = today - timedelta(days=days - 1 - i)
        weekend_factor = 0.35 if day.weekday() >= 5 else 1.0
        value *= (1 + growth_total / days)
        noise_factor = 1 + rng.uniform(-noise, noise)
        values.append(max(0.0, value * weekend_factor * noise_factor))
    return values


def _date_str(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


# ── Claude ────────────────────────────────────────────────────────────────────

def generate_claude_metrics(days: int) -> dict[str, Any]:
    req_series   = _daily_series(280, days, seed=1)
    cost_series  = _daily_series(3.80, days, seed=2)

    models = {
        "claude-3-5-sonnet-20241022": (0.58, 0.52),
        "claude-opus-4-6":            (0.15, 0.36),
        "claude-haiku-4-5-20251001":  (0.27, 0.12),
    }

    daily_trend: list[dict[str, Any]] = []
    model_breakdown: dict[str, dict[str, Any]] = {m: {"requests": 0, "cost_usd": 0.0} for m in models}
    total_requests = 0
    total_input_tokens = 0
    total_output_tokens = 0
    total_cost_usd = 0.0

    for i, (req_f, cost_f) in enumerate(zip(req_series, cost_series)):
        d = _date_str(days - 1 - i)
        req  = max(0, int(round(req_f)))
        cost = round(cost_f, 4)
        inp  = req * 1800
        out  = req * 430

        daily_trend.append({"date": d, "requests": req, "cost_usd": cost, "tokens": inp + out})
        total_requests      += req
        total_input_tokens  += inp
        total_output_tokens += out
        total_cost_usd      += cost

        for model, (req_share, cost_share) in models.items():
            model_breakdown[model]["requests"]  += max(0, int(req * req_share))
            model_breakdown[model]["cost_usd"]   = round(
                model_breakdown[model]["cost_usd"] + cost * cost_share, 4)

    return {
        "platform": "claude",
        "period_days": days,
        "total_requests": total_requests,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cost_usd": round(total_cost_usd, 2),
        "active_users": None,
        "model_breakdown": model_breakdown,
        "daily_trend": daily_trend,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "_mock": True,
    }


# ── ChatGPT ───────────────────────────────────────────────────────────────────

def generate_openai_metrics(days: int) -> dict[str, Any]:
    req_series  = _daily_series(2200, days, seed=10)
    cost_series = _daily_series(17.50, days, seed=11)

    models = {
        "gpt-4o":      (0.35, 0.62),
        "gpt-4o-mini": (0.55, 0.20),
        "o3-mini":     (0.10, 0.18),
    }

    daily_trend: list[dict[str, Any]] = []
    model_breakdown: dict[str, dict[str, Any]] = {m: {"requests": 0, "cost_usd": 0.0} for m in models}
    total_requests = 0
    total_input_tokens = 0
    total_output_tokens = 0
    total_cost_usd = 0.0

    for i, (req_f, cost_f) in enumerate(zip(req_series, cost_series)):
        d = _date_str(days - 1 - i)
        req  = max(0, int(round(req_f)))
        cost = round(cost_f, 4)
        inp  = req * 950
        out  = req * 140

        daily_trend.append({"date": d, "requests": req, "cost_usd": cost, "tokens": inp + out})
        total_requests      += req
        total_input_tokens  += inp
        total_output_tokens += out
        total_cost_usd      += cost

        for model, (req_share, cost_share) in models.items():
            model_breakdown[model]["requests"]  += max(0, int(req * req_share))
            model_breakdown[model]["cost_usd"]   = round(
                model_breakdown[model]["cost_usd"] + cost * cost_share, 4)

    return {
        "platform": "chatgpt",
        "period_days": days,
        "total_requests": total_requests,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cost_usd": round(total_cost_usd, 2),
        "active_users": None,
        "model_breakdown": model_breakdown,
        "daily_trend": daily_trend,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "_mock": True,
    }


# ── M365 Copilot ──────────────────────────────────────────────────────────────

def generate_copilot_metrics(days: int) -> dict[str, Any]:
    licensed_seats = 2800
    active_users   = 1842

    app_breakdown = {
        "teams":      1842,
        "outlook":    1567,
        "word":       1204,
        "excel":       987,
        "powerpoint":  634,
    }

    adoption_rate = round((active_users / licensed_seats) * 100, 2)

    return {
        "platform": "m365_copilot",
        "period_days": days,
        "active_users": active_users,
        "licensed_seats": licensed_seats,
        "adoption_rate_pct": adoption_rate,
        "app_breakdown": app_breakdown,
        "total_requests": None,
        "total_cost_usd": 0.0,
        "daily_trend": [],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "_mock": True,
    }


# ── Dispatch ──────────────────────────────────────────────────────────────────

_GENERATORS = {
    "claude":       generate_claude_metrics,
    "chatgpt":      generate_openai_metrics,
    "m365_copilot": generate_copilot_metrics,
}


def generate(platform: str, days: int) -> dict[str, Any]:
    fn = _GENERATORS.get(platform)
    if fn is None:
        raise ValueError(f"Unknown platform for mock data: {platform}")
    return fn(days)
