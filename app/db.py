# app/db.py
import os
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from psycopg_pool import ConnectionPool
from psycopg import Connection

# ======================================================================================
# DSN / Conninfo
#   Lee SUPABASE_DB_URL o DATABASE_URL.
#   Si no trae sslmode, se fuerza a "require".
# ======================================================================================

def _conninfo() -> str:
    url = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("Falta SUPABASE_DB_URL/DATABASE_URL en el entorno.")
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url

# ======================================================================================
# Pool ÚNICO GLOBAL (válvula contra el pooler de Supabase)
#   - max_size controlado por ENV DB_POOL_MAX (default 4)
#   - timeouts razonables
#   - keepalives para evitar cierres silenciosos
#   - open=False: lo abrimos en startup y lo cerramos en shutdown
# ======================================================================================

_POOL: Optional[ConnectionPool] = None

def get_pool() -> ConnectionPool:
    """Devuelve SIEMPRE el mismo pool global para todo el proceso."""
    global _POOL
    if _POOL is None:
        _POOL = ConnectionPool(
            _conninfo(),
            min_size=1,
            max_size=int(os.getenv("DB_POOL_MAX", "4")),  # ⚙️ ajustable por ENV
            timeout=5,          # seg para acquire desde el pool
            max_waiting=16,     # cola máxima de esperas antes de rechazar
            max_lifetime=300,   # recicla conexiones viejas
            max_idle=60,        # cierra ociosas
            kwargs={
                "autocommit": False,
                "application_name": "backpedidos",
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 5,
                # timeouts del lado servidor (ajustables)
                "options": "-c statement_timeout=30000 -c idle_in_transaction_session_timeout=10000",
            },
            open=False,         # 👉 se abre explícitamente en startup
        )
    return _POOL

def open_pool() -> None:
    """Abrí el pool en startup (FastAPI)."""
    get_pool().open()

def close_pool() -> None:
    """Cerrá el pool en shutdown (FastAPI)."""
    p = get_pool()
    try:
        p.close()
    except Exception:
        pass

# ======================================================================================
# Compat: contextmanager para obtener una conexión del pool
# ======================================================================================

@contextmanager
def get_conn() -> Iterator[Connection]:
    """
    Uso:
      with get_conn() as conn:
          with conn.cursor() as cur:
              cur.execute("SELECT 1")
              conn.commit()
    """
    with get_pool().connection() as conn:  # type: ignore[assignment]
        yield conn  # retorno al pool garantizado

# ======================================================================================
# Healthcheck y warmup opcional
# ======================================================================================

def healthcheck() -> bool:
    """SELECT 1 contra la base; True si responde, False si falla (no levanta excepción)."""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except Exception:
        return False

def _warmup(attempts: int = 1, delay: float = 0.0) -> None:
    """Warm-up del pool (opcional) para evitar spike en el primer request."""
    for _ in range(attempts):
        try:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            break
        except Exception:
            time.sleep(delay)

# Llamá open_pool() en app.main startup; close_pool() en shutdown.
# Si querés “despertar” la conexión al inicio, podés llamar _warmup(1) en el startup.
# _warmup(1)
