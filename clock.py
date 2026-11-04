"""Clock abstraction used by scheduling logic and deterministic tests."""
from datetime import datetime, timezone


class Clock:
    def now(self) -> datetime:
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class DatabaseClock(Clock):
    """Clock backed by PostgreSQL now(), used by runtime deadline checks."""
    def __init__(self, connection_factory):
        self.connection_factory=connection_factory

    @staticmethod
    def now_on(conn) -> datetime:
        with conn.cursor() as cur:
            cur.execute("SELECT now()")
            value=cur.fetchone()[0]
        if value.tzinfo is None:
            raise RuntimeError("PostgreSQL now() returned a naive timestamp")
        return value.astimezone(timezone.utc)

    def now(self) -> datetime:
        with self.connection_factory() as conn:
            return self.now_on(conn)


class FrozenClock(Clock):
    def __init__(self, value: datetime):
        if value.tzinfo is None:
            raise ValueError("FrozenClock requires an aware datetime")
        self._value = value.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._value

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("FrozenClock requires an aware datetime")
        self._value = value.astimezone(timezone.utc)

    def advance_seconds(self, seconds: float) -> None:
        from datetime import timedelta
        self._value += timedelta(seconds=seconds)
