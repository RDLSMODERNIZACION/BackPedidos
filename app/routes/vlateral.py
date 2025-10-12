# app/routes/vlateral.py
# Endpoint de lectura para la vista:
#   - public.v_pedido_info  →  GET /ui/pedidos/{pedido_id:int}/info
# ------------------------------------------------------------------

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
import os
import psycopg
from psycopg.rows import dict_row

router = APIRouter(prefix="/ui", tags=["ui"])

# ---------- DB helpers ----------
def _db_url() -> str:
    url = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("Falta SUPABASE_DB_URL/DATABASE_URL en el entorno")
    return url

def _get_conn():
    return psycopg.connect(_db_url(), row_factory=dict_row)

SQL_INFO_BY_ID = """
SELECT
  id,
  numero,
  fecha_pedido,
  fecha_desde,
  fecha_hasta,
  presupuesto_estimado,
  observaciones,
  modulo_payload,
  ambito_payload
FROM public.v_pedido_info
WHERE id = %s
"""

@router.get("/pedidos/{pedido_id:int}/info")
def get_pedido_info(pedido_id: int):
    """
    Devuelve datos normalizados del pedido desde v_pedido_info:
    - id, numero
    - fechas (pedido, desde, hasta)
    - presupuesto_estimado, observaciones
    - modulo_payload (JSON), ambito_payload (JSON)
    """
    try:
        with _get_conn() as con, con.cursor() as cur:
            cur.execute(SQL_INFO_BY_ID, (pedido_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Pedido {pedido_id} no encontrado")
            return jsonable_encoder(row)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"v_pedido_info_error: {e}")
