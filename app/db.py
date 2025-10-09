import os
from psycopg_pool import ConnectionPool

# Usa SUPABASE_DB_URL o DATABASE_URL (driver psycopg)
DB_URL = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
if not DB_URL:
    raise RuntimeError("Falta SUPABASE_DB_URL/DATABASE_URL en el entorno.")

# Pool (conexiones reusables)
pool = ConnectionPool(
    conninfo=DB_URL,
    max_size=10,
    kwargs={"autocommit": False},  # trabajamos en transacciones
)

def get_conn():
    # Context manager yields a psycopg.Connection
    return pool.connection()
