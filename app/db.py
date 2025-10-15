# app/db.py
import os
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from psycopg_pool import ConnectionPool
from psycopg import Connection, OperationalError, InterfaceError

_POOL: Optional[ConnectionPool] = None

def _conninfo() -> str:
    url = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("Falta SUPABASE_DB_URL/DATABASE_URL en el entorno.")
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url

def _make_pool() -> ConnectionPool:
    return ConnectionPool(
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

def get_pool() -> ConnectionPool:
    global _POOL
    if _POOL is None:
        _POOL = _make_pool()
    return _POOL

def open_pool() -> None:
    """
    Idempotente: siempre deja un pool NUEVO y abierto.
    Evita 'pool has already been opened/closed and cannot be reused'.
    """
    global _POOL
    # siempre cerramos cualquier pool previo y recreamos
    try:
        if _POOL is not None:
            _POOL.close()
    except Exception:
        pass
    _POOL = _make_pool()
    _POOL.open()

def close_pool() -> None:
    """
    Idempotente: cierra si existe y limpia la referencia global.
    """
    global _POOL
    try:
        if _POOL is not None:
            _POOL.close()
    except Exception:
        pass
    finally:
        _POOL = None

@contextmanager
def get_conn() -> Iterator[Connection]:
    """
    Toma una conexión del pool con un reintento:
    - Si el pooler cortó la conexión, recreamos el pool y reintentamos una vez.
    """
    global _POOL
    try:
        with get_pool().connection() as conn:  # type: ignore[assignment]
            yield conn
    except (OperationalError, InterfaceError):
        # Recrear el pool y reintentar
        try:
            if _POOL is not None:
                _POOL.close()
        except Exception:
            pass
        _POOL = _make_pool()
        _POOL.open()
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
