"""PostgreSQL connection helper. psycopg 3 is an optional scheduler dependency."""
import os


def connect():
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("Postgres scheduler dependency missing; install psycopg[binary]>=3.2,<4") from exc
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError("DATABASE_URL is required for the PostgreSQL scheduler")
    return psycopg.connect(dsn, autocommit=False)
