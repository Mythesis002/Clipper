"""Clock abstraction used by scheduling logic and deterministic tests."""
from datetime import datetime, timezone


class Clock:
    def now(self) -> datetime:
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


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
