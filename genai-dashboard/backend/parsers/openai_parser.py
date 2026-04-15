# openai_parser.py — Parse usage/cost CSV exports from the OpenAI Platform
#
# How to export:
#   1. Log in at https://platform.openai.com
#   2. Go to Usage (left sidebar)
#   3. Select your date range, then click "Export" → CSV
#   For billing/cost data: Billing → Usage → Export
#
# Supported export formats:
#
# Format A — Usage export (one row per model per day):
#   Date, Model, Requests, Input tokens, Output tokens, Cost ($)
#
# Format B — Aggregated usage export (no model column):
#   Date, Requests, Context token count, Generated token count, Cost ($)
#
# Format C — Billing line-items export:
#   Date, Description, Usage, Cost
#
# All formats are auto-detected from the headers.
# Also accepts JSON array.

import csv
import io
import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column-name aliases
# ---------------------------------------------------------------------------

_DATE_COLS = {"date", "period", "report_date", "day", "timestamp", "created"}
_MODEL_COLS = {"model", "model_name", "model_id", "product", "description", "service"}
_INPUT_COLS = {
    "input_tokens", "input token count", "context_tokens",
    "context token count", "n_context_tokens_total", "prompt_tokens",
}
_OUTPUT_COLS = {
    "output_tokens", "output token count", "generated_tokens",
    "generated token count", "n_generated_tokens_total", "completion_tokens",
}
_REQUEST_COLS = {
    "requests", "request count", "n_requests", "num_requests",
    "api_requests", "total_requests",
}
_COST_COLS = {
    "cost", "cost ($)", "cost($)", "total cost", "amount", "spend",
    "cost_usd", "usd", "usage",
}


def _find(headers: list[str], aliases: set[str]) -> str | None:
    normalised = {h.lower().strip(): h for h in headers}
    for alias in aliases:
        if alias in normalised:
            return normalised[alias]
    return None


def _parse_date(raw: str) -> str:
    raw = raw.strip()
    for fmt in (
        "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d",
        "%b %d, %Y", "%B %d, %Y",
        "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    # Try just first 10 chars
    part = raw[:10]
    for sep in ("-", "/"):
        if sep in part:
            try:
                parts = part.split(sep)
                if len(parts) == 3:
                    if len(parts[0]) == 4:  # YYYY-MM-DD
                        return part.replace("/", "-")
                    else:  # MM/DD/YYYY
                        return f"{parts[2]}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"
            except Exception:
                pass
    return raw[:10]


def _rows_to_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total_requests: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    model_breakdown: dict[str, dict[str, Any]] = {}
    daily_map: dict[str, dict[str, Any]] = {}

    for row in rows:
        date_str = row.get("_date", "")
        model = str(row.get("_model", "unknown")).strip() or "unknown"
        req = int(row.get("_requests", 0))
        inp = int(row.get("_input_tokens", 0))
        out = int(row.get("_output_tokens", 0))
        cost = float(row.get("_cost", 0.0))

        total_requests += req
        total_input_tokens += inp
        total_output_tokens += out
        total_cost_usd += cost

        if model not in model_breakdown:
            model_breakdown[model] = {"requests": 0, "cost_usd": 0.0}
        model_breakdown[model]["requests"] += req
        model_breakdown[model]["cost_usd"] = round(model_breakdown[model]["cost_usd"] + cost, 6)

        if date_str:
            if date_str not in daily_map:
                daily_map[date_str] = {"date": date_str, "requests": 0, "cost_usd": 0.0, "tokens": 0}
            daily_map[date_str]["requests"] += req
            daily_map[date_str]["cost_usd"] = round(daily_map[date_str]["cost_usd"] + cost, 6)
            daily_map[date_str]["tokens"] += inp + out

    daily_trend = sorted(daily_map.values(), key=lambda x: x["date"])

    all_dates = sorted(daily_map.keys())
    if len(all_dates) >= 2:
        d0 = datetime.strptime(all_dates[0], "%Y-%m-%d")
        d1 = datetime.strptime(all_dates[-1], "%Y-%m-%d")
        period_days = max(1, (d1 - d0).days + 1)
    else:
        period_days = len(all_dates) or 1

    return {
        "platform": "chatgpt",
        "period_days": period_days,
        "total_requests": total_requests,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cost_usd": round(total_cost_usd, 4),
        "active_users": None,
        "model_breakdown": model_breakdown,
        "daily_trend": daily_trend,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def parse_openai_csv(content: bytes) -> dict[str, Any]:
    """
    Parse an OpenAI Platform usage export (CSV or JSON) and return a
    normalised metrics dict compatible with MetricsStorage.save_snapshot().

    Raises ValueError if the content cannot be parsed or contains no data rows.
    """
    text = content.decode("utf-8-sig").strip()

    # JSON branch
    if text.startswith("[") or text.startswith("{"):
        try:
            raw = json.loads(text)
            if isinstance(raw, dict):
                raw = raw.get("data", raw.get("usage", [raw]))
            rows = []
            for item in raw:
                # Handle OpenAI API usage response format
                ts = item.get("start_time", item.get("timestamp", 0))
                if isinstance(ts, (int, float)) and ts > 0:
                    date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                else:
                    date_str = _parse_date(str(item.get("date", "")))
                rows.append({
                    "_date":          date_str,
                    "_model":         str(item.get("model", item.get("snapshot_id", "unknown"))),
                    "_input_tokens":  int(item.get("input_tokens", item.get("n_context_tokens_total", 0))),
                    "_output_tokens": int(item.get("output_tokens", item.get("n_generated_tokens_total", 0))),
                    "_requests":      int(item.get("num_requests", item.get("requests", item.get("n_requests", 0)))),
                    "_cost":          float(item.get("cost", item.get("cost_usd", 0.0))),
                })
            if not rows:
                raise ValueError("No rows found in OpenAI JSON export")
            logger.info("openai_parser: parsed %d rows from JSON", len(rows))
            return _rows_to_metrics(rows)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError(f"openai_parser: JSON parse error: {exc}") from exc

    # CSV branch
    reader = csv.DictReader(io.StringIO(text))
    headers: list[str] = list(reader.fieldnames or [])
    if not headers:
        raise ValueError("openai_parser: CSV has no headers")

    col_map = {
        "date":     _find(headers, _DATE_COLS),
        "model":    _find(headers, _MODEL_COLS),
        "input":    _find(headers, _INPUT_COLS),
        "output":   _find(headers, _OUTPUT_COLS),
        "requests": _find(headers, _REQUEST_COLS),
        "cost":     _find(headers, _COST_COLS),
    }
    logger.debug("openai_parser: resolved columns: %s", col_map)

    def _get(row: dict[str, str], col: str | None) -> str:
        return row.get(col, "").strip() if col else ""

    rows_out = []
    for i, raw_row in enumerate(reader):
        try:
            cost_raw = _get(raw_row, col_map["cost"])
            # Strip currency symbols
            cost_raw = cost_raw.lstrip("$£€").replace(",", "").strip()
            rows_out.append({
                "_date":          _parse_date(_get(raw_row, col_map["date"])) if col_map["date"] else "",
                "_model":         _get(raw_row, col_map["model"]) or "unknown",
                "_input_tokens":  int(float(_get(raw_row, col_map["input"]) or 0)),
                "_output_tokens": int(float(_get(raw_row, col_map["output"]) or 0)),
                "_requests":      int(float(_get(raw_row, col_map["requests"]) or 0)),
                "_cost":          float(cost_raw or 0.0),
            })
        except (ValueError, TypeError) as exc:
            logger.warning("openai_parser: skipping row %d: %s", i + 2, exc)

    if not rows_out:
        raise ValueError("openai_parser: no valid data rows in CSV")

    logger.info("openai_parser: parsed %d rows from CSV", len(rows_out))
    return _rows_to_metrics(rows_out)
