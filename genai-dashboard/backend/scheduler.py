# scheduler.py — APScheduler background refresh for all GenAI platform metrics.
# Fires once immediately on startup, then every REFRESH_INTERVAL_HOURS hours.
# Individual source failures are isolated — one failure never stops the others.

import asyncio
import logging
import os
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from sources import AnthropicClient, GraphClient, OpenAIClient
from storage import MetricsStorage

logger = logging.getLogger(__name__)

_PERIODS = [7, 30, 90]


async def _fetch_one(name: str, client: AnthropicClient | OpenAIClient | GraphClient,
                     period: int, storage: MetricsStorage) -> None:
    try:
        data = await client.get_metrics(days=period)
        storage.save_snapshot(name, period, data)
        logger.info("scheduler: [%s] %dd — saved (%s)",
                    name, period, "mock" if data.get("_mock") else "live")
    except Exception as exc:
        logger.error("scheduler: [%s] %dd — FAILED: %s", name, period, exc, exc_info=True)


async def refresh_all(storage: MetricsStorage) -> None:
    logger.info("scheduler: starting refresh at %s", datetime.now(timezone.utc).isoformat())
    clients: list[tuple[str, AnthropicClient | OpenAIClient | GraphClient]] = []
    for name, cls in [("claude", AnthropicClient), ("chatgpt", OpenAIClient), ("m365_copilot", GraphClient)]:
        try:
            clients.append((name, cls()))
        except Exception as exc:
            logger.error("scheduler: could not create client for %s: %s", name, exc)

    tasks = [_fetch_one(name, client, period, storage)
             for period in _PERIODS for name, client in clients]
    await asyncio.gather(*tasks)
    logger.info("scheduler: refresh complete")


def create_scheduler(storage: MetricsStorage) -> AsyncIOScheduler:
    interval_hours = float(os.environ.get("REFRESH_INTERVAL_HOURS", "1"))
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        refresh_all,
        trigger=IntervalTrigger(hours=interval_hours),
        args=[storage],
        id="refresh_all",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
    )
    return scheduler
