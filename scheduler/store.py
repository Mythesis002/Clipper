"""StateStore interface and legacy JSON compatibility adapter.

Legacy pipeline state is deliberately retained during migration. The scheduler
uses normalized tables; this adapter is for existing tests/manual commands and
does not pretend JSON provides distributed locking.
"""
from abc import ABC, abstractmethod
import json
from pathlib import Path
from typing import Any


class StateStore(ABC):
    @abstractmethod
    def load(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def save(self, state: dict[str, Any]) -> None:
        ...


class JsonFileStateStore(StateStore):
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"episodes": {}, "clips": [], "next_id": 1}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)


class PostgresStateStore(StateStore):
    """Postgres-backed legacy payload adapter, preserving the pipeline state shape.

    Use transaction-local app.user_id for RLS. This is not the normalized slot
    store; slot state lives in slots/events tables.
    """
    def __init__(self, connection_factory, user_id: str):
        self.connection_factory = connection_factory
        self.user_id = str(user_id)

    def load(self) -> dict[str, Any]:
        with self.connection_factory() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('app.user_id', %s, true)", (self.user_id,))
                cur.execute("SELECT payload FROM state_meta WHERE user_id = %s", (self.user_id,))
                row = cur.fetchone()
                return row[0] if row else {"episodes": {}, "clips": [], "next_id": 1}

    def save(self, state: dict[str, Any]) -> None:
        with self.connection_factory() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('app.user_id', %s, true)", (self.user_id,))
                cur.execute(
                    """INSERT INTO state_meta(user_id,payload,revision,updated_at)
                       VALUES (%s,%s::jsonb,1,now())
                       ON CONFLICT(user_id) DO UPDATE
                       SET payload=EXCLUDED.payload, revision=state_meta.revision+1,
                           updated_at=now()""",
                    (self.user_id, json.dumps(state, ensure_ascii=False)),
                )
