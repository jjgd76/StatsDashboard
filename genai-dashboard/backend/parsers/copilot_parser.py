# copilot_parser.py — Parse M365 Copilot activity reports from the Microsoft 365 Admin Center
#
# How to export:
#   1. Log in at https://admin.microsoft.com
#   2. Go to Reports → Microsoft 365 Reports → Microsoft 365 Copilot
#      (or: Reports → Usage → Microsoft 365 Copilot)
#   3. Select the desired period (7, 30, 90, or 180 days)
#   4. Click "Export" to download the CSV
#
# Two report types are auto-detected:
#
# Type 1 — Summary / aggregate report (one row per date or one summary row):
#   Report Refresh Date, Report Period (Days), [App] Copilot Active Users, ...
#   E.g. columns: "Teams Copilot Active Users", "Word Copilot Active Users", etc.
#
# Type 2 — User-level activity report (one row per user):
#   Report Refresh Date, User Principal Name, Display Name, Last Activity Date,
#   Teams Copilot Active, Word Copilot Active, Excel Copilot Active,
#   PowerPoint Copilot Active, Outlook Copilot Active, OneNote Copilot Active, ...
#   (values are "Yes" / "No" or 1/0)
#
# The parser auto-detects the type from the presence of a "User Principal Name" column.
# Also accepts JSON.

import csv
import io
import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# App column aliases — keys are normalised internal names
_APP_ALIASES: dict[str, list[str]] = {
    "teams":       ["teams copilot active users", "teams copilot active", "microsoft teams",
                    "teams", "teams_copilot_active_users", "teams_copilot_active"],
    "word":        ["word copilot active users", "word copilot active", "word",
                    "word_copilot_active_users", "word_copilot_active"],
    "excel":       ["excel copilot active users", "excel copilot active", "excel",
                    "excel_copilot_active_users", "excel_copilot_active"],
    "powerpoint":  ["powerpoint copilot active users", "powerpoint copilot active",
                    "powerpoint", "ppt", "powerpoint_copilot_active_users"],
    "outlook":     ["outlook copilot active users", "outlook copilot active", "outlook",
                    "outlook_copilot_active_users", "outlook_copilot_active"],
}

_LICENSE_COLS = {
    "licensed_copilot_users", "assigned_licenses", "licensed users",
    "total licensed users", "enabled users", "enabled_users",
    "copilot_licensed_users", "licenses assigned",
}
_PERIOD_COLS = {
    "report period (days)", "report_period", "period", "period_days",
    "report period", "days",
}
_REFRESH_DATE_COLS = {
    "report refresh date", "report_refresh_date", "refresh_date",
    "report date", "date", "as_of_date",
}
_LAST_ACTIVITY_COLS = {
    "last activity date", "last_activity_date", "lastactivitydate",
}
_UPN_COLS = {"user principal name", "user_principal_name", "upn", "email", "username"}


def _find(headers: list[str], aliases: set[str]) -> str | None:
    norm = {h.lower().strip(): h for h in headers}
    for alias in aliases:
        if alias in norm:
            return norm[alias]
    return None


def _find_app_col(headers: list[str], app: str) -> str | None:
    norm = {h.lower().strip(): h for h in headers}
    for alias in _APP_ALIASES[app]:
        if alias in norm:
            return norm[alias]
    return None


def _parse_date(raw: str) -> str:
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d",
                "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw[:10]


def _is_active(val: str) -> bool:
    """Interpret 'Yes', '1', 'true', 'x', non-empty as active."""
    v = val.strip().lower()
    return v in {"yes", "1", "true", "x", "active"} or (v not in {"no", "0", "false", "", "none", "n/a"} and bool(v))


# ---------------------------------------------------------------------------
# Summary-report parser (aggregate rows)
# ---------------------------------------------------------------------------

def _parse_summary(rows: list[dict[str, str]], headers: list[str]) -> dict[str, Any]:
    """Parse an aggregate (summary) Copilot report."""
    app_cols = {app: _find_app_col(headers, app) for app in _APP_ALIASES}
    period_col = _find(headers, _PERIOD_COLS)
    license_col = _find(headers, _LICENSE_COLS)

    app_totals: dict[str, int] = {app: 0 for app in _APP_ALIASES}
    licensed_seats: int = 0
    period_days: int = 30
    last_date: str = ""

    for row in rows:
        # Period
        if period_col and row.get(period_col, "").strip():
            try:
                period_days = int(float(row[period_col].strip()))
            except ValueError:
                pass

        # Licensed seats
        if license_col and row.get(license_col, "").strip():
            try:
                licensed_seats = max(licensed_seats, int(float(row[license_col].strip())))
            except ValueError:
                pass

        # App active users
        for app, col in app_cols.items():
            if col and row.get(col, "").strip():
                try:
                    app_totals[app] = max(app_totals[app], int(float(row[col].strip())))
                except ValueError:
                    pass

        # Latest refresh date
        for dc in _REFRESH_DATE_COLS:
            col = _find(headers, {dc})
            if col and row.get(col, "").strip():
                last_date = _parse_date(row[col])
                break

    active_users = max(app_totals.values()) if any(app_totals.values()) else 0
    adoption_rate = round((active_users / licensed_seats) * 100, 2) if licensed_seats else 0.0

    return {
        "platform": "m365_copilot",
        "period_days": period_days,
        "active_users": active_users,
        "licensed_seats": licensed_seats,
        "adoption_rate_pct": adoption_rate,
        "app_breakdown": app_totals,
        "report_date": last_date,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# User-level report parser
# ---------------------------------------------------------------------------

def _parse_user_level(rows: list[dict[str, str]], headers: list[str]) -> dict[str, Any]:
    """Parse a per-user Copilot activity report."""
    app_cols = {app: _find_app_col(headers, app) for app in _APP_ALIASES}
    license_col = _find(headers, _LICENSE_COLS)
    last_activity_col = _find(headers, _LAST_ACTIVITY_COLS)
    period_col = _find(headers, _PERIOD_COLS)

    app_active: dict[str, int] = {app: 0 for app in _APP_ALIASES}
    active_users: int = 0
    licensed_seats: int = 0
    period_days: int = 30

    if period_col:
        for row in rows:
            val = row.get(period_col, "").strip()
            if val:
                try:
                    period_days = int(float(val))
                    break
                except ValueError:
                    pass

    if license_col:
        for row in rows:
            val = row.get(license_col, "").strip()
            if val:
                try:
                    licensed_seats = int(float(val))
                    break
                except ValueError:
                    pass

    for row in rows:
        user_active = False
        for app, col in app_cols.items():
            if col and _is_active(row.get(col, "")):
                app_active[app] += 1
                user_active = True
        if user_active:
            active_users += 1

    if not licensed_seats:
        licensed_seats = len(rows)

    adoption_rate = round((active_users / licensed_seats) * 100, 2) if licensed_seats else 0.0

    return {
        "platform": "m365_copilot",
        "period_days": period_days,
        "active_users": active_users,
        "licensed_seats": licensed_seats,
        "adoption_rate_pct": adoption_rate,
        "app_breakdown": app_active,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_copilot_csv(content: bytes) -> dict[str, Any]:
    """
    Parse a Microsoft 365 Copilot activity report (CSV or JSON) and return a
    normalised metrics dict compatible with MetricsStorage.save_snapshot().

    Auto-detects summary vs. per-user format.
    Raises ValueError if the content cannot be parsed.
    """
    text = content.decode("utf-8-sig").strip()

    # JSON branch
    if text.startswith("[") or text.startswith("{"):
        try:
            raw = json.loads(text)
            if isinstance(raw, dict):
                raw = raw.get("value", raw.get("data", [raw]))
            if not isinstance(raw, list):
                raw = [raw]

            # Detect user-level vs. summary from key names
            sample = raw[0] if raw else {}
            is_user_level = any(
                k.lower() in {"userprincipalname", "user_principal_name", "upn"}
                for k in sample.keys()
            )
            # Convert JSON objects to flat row dicts for the existing parsers
            rows = [{str(k): str(v) for k, v in item.items()} for item in raw]
            headers = list(rows[0].keys()) if rows else []
            if is_user_level:
                return _parse_user_level(rows, headers)
            else:
                return _parse_summary(rows, headers)
        except (json.JSONDecodeError, KeyError, TypeError, IndexError) as exc:
            raise ValueError(f"copilot_parser: JSON parse error: {exc}") from exc

    # CSV branch
    reader = csv.DictReader(io.StringIO(text))
    headers = list(reader.fieldnames or [])
    if not headers:
        raise ValueError("copilot_parser: CSV has no headers")

    rows_raw = list(reader)
    if not rows_raw:
        raise ValueError("copilot_parser: CSV has no data rows")

    # Auto-detect report type
    is_user_level = _find(headers, _UPN_COLS) is not None

    rows_str: list[dict[str, str]] = [{k: str(v) for k, v in row.items()} for row in rows_raw]

    if is_user_level:
        logger.info("copilot_parser: detected user-level report (%d users)", len(rows_str))
        return _parse_user_level(rows_str, headers)
    else:
        logger.info("copilot_parser: detected summary report (%d rows)", len(rows_str))
        return _parse_summary(rows_str, headers)
