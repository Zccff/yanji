"""Run with python -m app.workers.task_worker from backend/."""

import asyncio
import contextlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import aio_pika

from app.core.config import settings
from app.db.models import Base
from app.db.session import engine
from app.services.task_queue import KINDS, process_job, publish_pending, safe_error

logger = logging.getLogger("devflow.worker")
EXCHANGE = "devflow.jobs"
DEAD_EXCHANGE = "devflow.jobs.dead"
HEARTBEAT = Path(__file__).resolve().parents[3] / ".data" / "runtime" / "worker-heartbeat.json"


def heartbeat(status, error=None):
    try:
        HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
        temporary = HEARTBEAT.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps({"pid": os.getpid(), "instance_id": os.environ.get("DEVFLOW_WORKER_INSTANCE"), "status": status, "timestamp": datetime.now(timezone.utc).isoformat(), "error": error}), encoding="utf-8")
        temporary.replace(HEARTBEAT)
    except OSError:
        logger.warning("Cannot update worker heartbeat")


def parse_message(body):
    payload = json.loads(body)
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or payload.get("kind") not in KINDS:
        raise ValueError("Unsupported job message")
    return uuid.UUID(str(payload["job_id"]))


async def consume(message):
    try:
        job_id = parse_message(message.body)
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        logger.warning("Rejecting malformed task message")
        await message.reject(requeue=False)
        return
    try:
        status = await asyncio.to_thread(process_job, job_id)
        logger.info("job=%s outcome=%s", job_id, status)
        if status == "failed":
            await message.reject(requeue=False)
        else:
            # Retry/busy states have already been persisted for dispatcher recovery.
            await message.ack()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Task infrastructure failure: %s", safe_error(exc))
        await asyncio.sleep(max(1, settings.task_retry_seconds))
        if not message.processed:
            await message.nack(requeue=True)


async def serve(connection):
    channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
    await channel.set_qos(prefetch_count=max(1, settings.task_prefetch_count))
    exchange = await channel.declare_exchange(EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True)
    dead = await channel.declare_exchange(DEAD_EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True)
    for kind in sorted(KINDS):
        dead_queue = await channel.declare_queue(f"devflow.{kind}.dead", durable=True)
        await dead_queue.bind(dead, routing_key=kind)
        queue = await channel.declare_queue(f"devflow.{kind}", durable=True, arguments={"x-dead-letter-exchange": DEAD_EXCHANGE, "x-dead-letter-routing-key": kind})
        await queue.bind(exchange, routing_key=kind)
        await queue.consume(consume)

    async def publish(job_id, kind):
        message = aio_pika.Message(
            body=json.dumps({"schema_version": 1, "job_id": str(job_id), "kind": kind}).encode(),
            content_type="application/json", message_id=str(job_id),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        )
        confirmed = await exchange.publish(message, routing_key=kind, mandatory=True, timeout=10)
        if confirmed is False:
            raise ConnectionError("RabbitMQ did not confirm the task")

    logger.info("Worker ready; queues=%s", ",".join(sorted(KINDS)))
    while True:
        try:
            await publish_pending(publish)
            heartbeat("ready" if connection.connected.is_set() else "reconnecting")
        except Exception as exc:
            heartbeat("degraded", safe_error(exc))
            logger.warning("Dispatcher recovery pending: %s", safe_error(exc))
        await asyncio.sleep(max(0.2, settings.task_dispatch_interval_seconds))


async def main():
    if engine.dialect.name != "postgresql":
        raise RuntimeError("The durable Worker requires PostgreSQL; SQLite is only a unit-test adapter")
    Base.metadata.create_all(bind=engine)
    while True:
        try:
            connection = await aio_pika.connect_robust(settings.rabbitmq_url, timeout=5, reconnect_interval=5)
            try:
                await serve(connection)
            finally:
                await connection.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            heartbeat("disconnected", safe_error(exc))
            logger.warning("RabbitMQ unavailable, retrying: %s", safe_error(exc))
            await asyncio.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("aio_pika").setLevel(logging.WARNING)
    logging.getLogger("aiormq").setLevel(logging.WARNING)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
