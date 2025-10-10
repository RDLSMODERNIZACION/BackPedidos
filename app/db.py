# app/db.py
import os
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from psycopg_pool import ConnectionPool
from psycopg import Connection

# --------------------------------------------------------------------------------------
# URL de conexión
# Recomendado: usar la URL del POOLER **Transaction** de Supabase:
#   postgres://USER:PWD@<region>.pooler.supabase.com:5432/postgres?sslmode=require
# (También podés usar SUPABASE_DB_URL; si no, DATABASE_URL.)
# --------------------------------------------------------------------------------------
DB_URL = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
if not DB_URL:
    raise RuntimeError("Falta SUPABASE_DB_URL/DATABASE_URL en el entorno.")

# --------------------------------------------------------------------------------------
# Pool MUY chico (estás detrás de un pooler externo). Mantiene vivas las conexiones.
# - min_size=0: permite que Render “hiberne” sin conexiones abiertas
# - max_size=2: suficiente para la app actual y evita saturar el pooler
# - timeout: cuánto esperar por una conexión del pool
# - max_waiting: cuántos en cola esperando antes de rechazar
# - keepalives: evita cierres silenciosos
# - options: timeouts de servidor (statement e in-tx idle)
# --------------------------------------------------------------------------------------
pool: ConnectionPool = ConnectionPool(
    conninfo=DB_URL,
    min_size=0,
    max_size=2,
    timeout=5,
    max_waiting=16,
    kwargs={
        "autocommit": False,
        "application_name": "backpedidos",
        "sslmode": "require",
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
        # timeouts del lado servidor (ajustables)
        "options": "-c statement_timeout=30000 -c idle_in_transaction_session_timeout=10000",
    },
)

@contextmanager
def get_conn() -> Iterator[Connection]:
    """
    Uso:
      with get_conn() as conn:
          with conn.cursor() as cur:
              cur.execute("SELECT 1")
              conn.commit()
    """
    with pool.connection() as conn:  # type: ignore[assignment]
        yield conn  # cierre/retorno al pool garantizado


def healthcheck() -> bool:
    """
    Ejecuta un SELECT 1 contra la base para verificar el pool.
    Devolvé True/False; no levanta excepción (útil para /healthz).
    """
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except Exception:
        return False


# (Opcional) warm-up del pool (evita spike en el primer request)
def _warmup(attempts: int = 1, delay: float = 0.0) -> None:
    for i in range(attempts):
        try:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            break
        except Exception:
            time.sleep(delay)


# Llamá _warmup(1) si querés “despertar” la conexión en el startup
# _warmup(1)
