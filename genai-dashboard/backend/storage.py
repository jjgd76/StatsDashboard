# storage.py — Azure Table Storage persistence layer for metrics snapshots and raw history
#
# Tables used:
#   MetricsCache — latest snapshot per (platform, period). Enables fast dashboard loads.
#   MetricsRaw   — full history of every fetch. Enables trend queries.
#
# Authentication: uses AZURE_STORAGE_CONNECTION_STRING if set, otherwise falls back to
# DefaultAzureCredential with AZURE_STORAGE_ACCOUNT_NAME (for managed identity / local az login).

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableClient, TableServiceClient

logger = logging.getLogger(__name__)

_CACHE_TABLE = "MetricsCache"
_RAW_TABLE = "MetricsRaw"
_PERIOD_MAP = {7: "7d", 30: "30d", 90: "90d"}


def _period_key(period_days: int) -> str:
    return _PERIOD_MAP.get(period_days, f"{period_days}d")


def _serialise(data: dict[str, Any]) -> dict[str, str]:
    """Flatten a metrics dict into a single JSON string column for Table Storage."""
    return {"payload": json.dumps(data, default=str)}


def _deserialise(entity: dict[str, Any]) -> dict[str, Any] | None:
    raw = entity.get("payload")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("storage: JSON decode error: %s", exc)
        return None


class MetricsStorage:
    """Azure Table Storage client for caching and persisting metrics snapshots."""

    def __init__(self) -> None:
        conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        if conn_str:
            self._service: TableServiceClient = TableServiceClient.from_connection_string(conn_str)
        else:
            account_name = os.environ["AZURE_STORAGE_ACCOUNT_NAME"]
            from azure.identity import DefaultAzureCredential
            credential = DefaultAzureCredential()
            endpoint = f"https://{account_name}.table.core.windows.net"
            self._service = TableServiceClient(endpoint=endpoint, credential=credential)

        self._ensure_tables()

    # ------------------------------------------------------------------
    # Table initialisation
    # ------------------------------------------------------------------

    def _ensure_tables(self) -> None:
        for table_name in (_CACHE_TABLE, _RAW_TABLE):
            try:
                self._service.create_table(table_name)
                logger.info("storage: created table %s", table_name)
            except ResourceExistsError:
                pass  # already exists — fine
            except Exception as exc:
                logger.error("storage: could not create table %s: %s", table_name, exc)

    def _cache_client(self) -> TableClient:
        return self._service.get_table_client(_CACHE_TABLE)

    def _raw_client(self) -> TableClient:
        return self._service.get_table_client(_RAW_TABLE)

    # ------------------------------------------------------------------
    # Write helpers
    # ------------------------------------------------------------------

    def save_snapshot(self, platform: str, period_days: int, data: dict[str, Any]) -> None:
        """Upsert the latest snapshot into MetricsCache and append to MetricsRaw."""
        row_key = _period_key(period_days)
        ts = datetime.now(timezone.utc).isoformat()

        cache_entity = {
            "PartitionKey": platform,
            "RowKey": row_key,
            "updated_at": ts,
            **_serialise(data),
        }
        try:
            self._cache_client().upsert_entity(cache_entity)
            logger.debug("storage: upserted MetricsCache[%s/%s]", platform, row_key)
        except Exception as exc:
            logger.error("storage: MetricsCache upsert failed for %s/%s: %s", platform, row_key, exc)

        # Raw history — row key is timestamp to guarantee uniqueness
        raw_entity = {
            "PartitionKey": platform,
            "RowKey": ts.replace(":", "-").replace(".", "-"),
            "period_days": str(period_days),
            **_serialise(data),
        }
        try:
            self._raw_client().create_entity(raw_entity)
            logger.debug("storage: appended MetricsRaw[%s/%s]", platform, raw_entity["RowKey"])
        except Exception as exc:
            logger.error("storage: MetricsRaw append failed for %s: %s", platform, exc)

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def get_snapshot(self, platform: str, period_days: int) -> dict[str, Any] | None:
        """Return the cached snapshot for the given platform and period, or None."""
        row_key = _period_key(period_days)
        try:
            entity = self._cache_client().get_entity(partition_key=platform, row_key=row_key)
            return _deserialise(entity)
        except ResourceNotFoundError:
            return None
        except Exception as exc:
            logger.error("storage: MetricsCache read failed for %s/%s: %s", platform, row_key, exc)
            return None

    def get_all_snapshots(self, period_days: int) -> list[dict[str, Any]]:
        """Return cached snapshots for all platforms for the given period."""
        platforms = ["claude", "chatgpt", "m365_copilot"]
        results: list[dict[str, Any]] = []
        for platform in platforms:
            snap = self.get_snapshot(platform, period_days)
            if snap:
                results.append(snap)
        return results

    def get_trend(self, platform: str, days_back: int = 90) -> list[dict[str, Any]]:
        """Query MetricsRaw for historical snapshots, returning the most recent ones."""
        try:
            client = self._raw_client()
            # Filter by PartitionKey; take all rows for this platform then sort/limit
            query = f"PartitionKey eq '{platform}'"
            entities = list(client.query_entities(query_filter=query))
            # Sort descending by RowKey (ISO timestamp with colons replaced by dashes)
            entities.sort(key=lambda e: e["RowKey"], reverse=True)
            # Return up to days_back entries
            results = []
            for entity in entities[:days_back]:
                data = _deserialise(entity)
                if data:
                    results.append(data)
            results.reverse()  # chronological order
            return results
        except Exception as exc:
            logger.error("storage: MetricsRaw trend query failed for %s: %s", platform, exc)
            return []
