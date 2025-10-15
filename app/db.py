# app/db.py
import os
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from psycopg_pool import ConnectionPool
from psycopg import Connection, OperationalError, InterfaceError  # ⬅️ importa errores

def _conninfo() -> str:
    url = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("Falta SUPABASE_DB_URL/DATABASE_URL en el entorno.")
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url

_POOL: Optional[ConnectionPool] = None

def get_pool() -> ConnectionPool:
    global _POOL
    if _POOL is None:
        _POOL = ConnectionPool(
            _conninfo(),
            min_size=1,
            max_size=int(os.getenv("DB_POOL_MAX", "4")),
            timeout=5,
            max_waiting=16,
            max_lifetime=300,
            max_idle=60,
            kwargs={
                "autocommit": False,
                "application_name": "backpedidos",
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 5,
                "options": "-c statement_timeout=30000 -c idle_in_transaction_session_timeout=10000",
            },
            open=False,
        )
    return _POOL

def open_pool() -> None:
    get_pool().open()

def close_pool() -> None:
    p = get_pool()
    try:
        p.close()
    except Exception:
        pass

@contextmanager
def get_conn() -> Iterator[Connection]:
    """
    Toma una conexión del pool con un reintento automático si el pooler cerró la conexión.
    """
    try:
        with get_pool().connection() as conn:  # type: ignore[assignment]
            yield conn
    except (OperationalError, InterfaceError):
        # reconectar el pool y un intento más
        try:
            close_pool()
        except Exception:
            pass
        open_pool()
        with get_pool().connection() as conn:  # type: ignore[assignment]
            yield conn

def healthcheck() -> bool:
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except Exception:
        return False

def _warmup(attempts: int = 1, delay: float = 0.0) -> None:
    for _ in range(attempts):
        try:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            break
        except Exception:
            time.sleep(delay)
