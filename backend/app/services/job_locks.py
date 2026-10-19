"""Session locks survive service-level commits and release on process death."""

import hashlib
import threading
from contextlib import contextmanager

from sqlalchemy import text


class JobResourceBusy(RuntimeError):
    pass


_local_locks: dict[tuple[int, str], threading.Lock] = {}
_local_guard = threading.Lock()


@contextmanager
def resource_lock(bind, resource: str):
    engine = getattr(bind, "engine", bind)
    if engine.dialect.name == "postgresql":
        key = int.from_bytes(hashlib.sha256(resource.encode()).digest()[:8], "big", signed=True)
        with engine.connect() as connection:
            acquired = connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar()
            connection.commit()
            if not acquired:
                raise JobResourceBusy(resource)
            try:
                yield
            finally:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                connection.commit()
    elif engine.dialect.name == "sqlite":
        # In-memory SQLite is a single-process test adapter, not the worker backend.
        with _local_guard:
            lock = _local_locks.setdefault((id(engine), resource), threading.Lock())
        if not lock.acquire(blocking=False):
            raise JobResourceBusy(resource)
        try:
            yield
        finally:
            lock.release()
    else:
        raise RuntimeError("Background workers require PostgreSQL resource locks")
