# anthropic_parser.py — Parse usage CSV exports from the Anthropic Console
#
# How to export:
#   1. Log in at https://console.anthropic.com
#   2. Go to Settings → Usage
#   3. Select your date range and click "Export CSV"
#
# Expected CSV columns (column names are matched case-insensitively):
#   date / timestamp / report_date
#   model / model_name
#   input_tokens / total_input_tokens / prompt_tokens
#   output_tokens / total_output_tokens / completion_tokens
#   cache_creation_input_tokens  (optional)
#   cache_read_input_tokens      (optional)
#   requests / n_requests / request_count  (optional — inferred as 1 if absent)
#   cost / cost_usd / spend / amount       (optional)
#
# Also accepts JSON: an array of objects with the same field names.

import csv
import io
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column-name aliases
# ---------------------------------------------------------------------------

_DATE_COLS = {"date", "timestamp", "report_date", "period_start", "day", "created_at"}
_MODEL_COLS = {"model", "model_name", "model_id", "model_slug"}
_INPUT_COLS = {
    "input_tokens", "total_input_tokens", "prompt_tokens",
    "context_tokens", "n_context_tokens_total",
}
_OUTPUT_COLS = {
    "output_tokens", "total_output_tokens", "completion_tokens",
    "generated_tokens", "n_generated_tokens_total",
}
_CACHE_CREATE_COLS = {"cache_creation_input_tokens", "cache_write_tokens", "cache_creation_tokens"}
_CACHE_READ_COLS = {"cache_read_input_tokens", "cache_read_tokens"}
_REQUEST_COLS = {"requests", "n_requests", "request_count", "total_requests", "count", "num_requests"}
_COST_COLS = {"cost", "cost_usd", "spend", "total_cost", "amount", "cost_($)", "cost ($)", "usd"}


def _find(headers: list[str], aliases: set[str]) -> str | None:
    """Return the first header that matches any alias (case-insensitive)."""
    normalised = {h.lower().strip().replace(" ", "_"): h for h in headers}
    for alias in aliases:
        if alias in normalised:
            return normalised[alias]
    return None


def _parse_date(raw: str) -> str:
    """Return YYYY-MM-DD from various date string formats."""
    raw = raw.strip()
    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%m/%d/%Y",
        "%d/%m/%Y",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(raw[:len(fmt.replace("%Y", "0000").replace("%m", "00")
                                         .replace("%d", "00").replace("%H", "00")
                                         .replace("%M", "00").replace("%S", "00")
                                         .replace("%z", "")
                                         )], fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    # Fallback: just take first 10 chars if they look like a date
    if len(raw) >= 10 and raw[4] in ("-", "/") and raw[7] in ("-", "/"):
        return raw[:10].replace("/", "-")
    return raw[:10]


def _rows_to_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate a list of flat row dicts into the standard metrics dict."""
    total_requests: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    model_breakdown: dict[str, dict[str, Any]] = {}
    daily_map: dict[str, dict[str, Any]] = {}

    for row in rows:
        date_str: str = row.get("_date", "")
        model: str = str(row.get("_model", "unknown")).strip() or "unknown"
        req: int = int(row.get("_requests", 1))
        inp: int = int(row.get("_input_tokens", 0))
        out: int = int(row.get("_output_tokens", 0))
        cost: float = float(row.get("_cost", 0.0))

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

    # Determine period from date range
    all_dates = sorted(daily_map.keys())
    if len(all_dates) >= 2:
        d0 = datetime.strptime(all_dates[0], "%Y-%m-%d")
        d1 = datetime.strptime(all_dates[-1], "%Y-%m-%d")
        period_days = max(1, (d1 - d0).days + 1)
    else:
        period_days = len(all_dates) or 1

    return {
        "platform": "claude",
        "period_days": period_days,
        "total_requests": total_requests,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_cost_usd": round(total_cost_usd, 4),
        "active_users": None,  # not available in usage export
        "model_breakdown": model_breakdown,
        "daily_trend": daily_trend,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _normalise_csv_row(row: dict[str, str], col_map: dict[str, str | None]) -> dict[str, Any]:
    """Map a raw CSV row through the resolved column map into internal field names."""
    def _get(col: str | None) -> str:
        return row.get(col, "").strip() if col else ""

    return {
        "_date":         _parse_date(_get(col_map["date"])) if col_map["date"] else "",
        "_model":        _get(col_map["model"]) or "unknown",
        "_input_tokens": int(float(_get(col_map["input"]) or 0)),
        "_output_tokens": int(float(_get(col_map["output"]) or 0)),
        "_requests":     int(float(_get(col_map["requests"]) or 1)),
        "_cost":         float(_get(col_map["cost"]) or 0.0),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_anthropic_csv(content: bytes) -> dict[str, Any]:
    """
    Parse an Anthropic Console usage export (CSV or JSON) and return a
    normalised metrics dict compatible with MetricsStorage.save_snapshot().

    Raises ValueError if the content cannot be parsed or contains no data rows.
    """
    text = content.decode("utf-8-sig").strip()  # strip BOM

    # Try JSON first
    if text.startswith("[") or text.startswith("{"):
        try:
            raw = json.loads(text)
            if isinstance(raw, dict):
                raw = raw.get("data", raw.get("usage", [raw]))
            rows = []
            for item in raw:
                rows.append({
                    "_date":          _parse_date(str(item.get("date", item.get("timestamp", "")))),
                    "_model":         str(item.get("model", item.get("model_name", "unknown"))),
                    "_input_tokens":  int(item.get("input_tokens", item.get("prompt_tokens", 0))),
                    "_output_tokens": int(item.get("output_tokens", item.get("completion_tokens", 0))),
                    "_requests":      int(item.get("requests", item.get("n_requests", 1))),
                    "_cost":          float(item.get("cost", item.get("cost_usd", item.get("spend", 0.0)))),
                })
            if not rows:
                raise ValueError("No rows found in Anthropic JSON export")
            logger.info("anthropic_parser: parsed %d rows from JSON", len(rows))
            return _rows_to_metrics(rows)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError(f"anthropic_parser: JSON parse error: {exc}") from exc

    # Parse CSV
    reader = csv.DictReader(io.StringIO(text))
    headers: list[str] = list(reader.fieldnames or [])
    if not headers:
        raise ValueError("anthropic_parser: CSV has no headers")

    col_map = {
        "date":     _find(headers, _DATE_COLS),
        "model":    _find(headers, _MODEL_COLS),
        "input":    _find(headers, _INPUT_COLS),
        "output":   _find(headers, _OUTPUT_COLS),
        "requests": _find(headers, _REQUEST_COLS),
        "cost":     _find(headers, _COST_COLS),
    }
    logger.debug("anthropic_parser: resolved columns: %s", col_map)

    rows = []
    for i, raw_row in enumerate(reader):
        try:
            rows.append(_normalise_csv_row(raw_row, col_map))
        except (ValueError, TypeError) as exc:
            logger.warning("anthropic_parser: skipping row %d: %s", i + 2, exc)

    if not rows:
        raise ValueError("anthropic_parser: no valid data rows in CSV")

    logger.info("anthropic_parser: parsed %d rows from CSV", len(rows))
    return _rows_to_metrics(rows)
