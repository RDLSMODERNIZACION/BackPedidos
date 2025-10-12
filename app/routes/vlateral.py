# app/routes/vlateral.py
# Endpoints de lectura para vistas:
#   - public.v_pedido_info          → GET /ui/pedidos/{pedido_id:int}/info
#   - public.v_ui_pedido_archivos   → GET /ui/pedidos/{pedido_id:int}/archivos
# ------------------------------------------------------------------

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from typing import Any, Dict, List, Optional
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

def _iso(dt: Optional[Any]) -> Optional[str]:
    try:
        return dt.isoformat() if dt is not None else None
    except Exception:
        return dt  # si ya es str u otro tipo

# ---------- SQL ----------
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

SQL_FILES_BY_PEDIDO = """
SELECT
  id,
  pedido_id,
  kind,
  filename,
  content_type,
  size_bytes,
  uploaded_at,
  review_status,
  review_notes,
  reviewed_by,
  reviewed_at,
  url
FROM public.v_ui_pedido_archivos
WHERE pedido_id = %s
ORDER BY uploaded_at DESC NULLS LAST, id DESC
"""

# ---------- Endpoints ----------

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

@router.get("/pedidos/{pedido_id:int}/archivos")
def get_pedido_archivos(pedido_id: int):
    """
    Lista adjuntos desde v_ui_pedido_archivos para un pedido.
    Respuesta: { items: [{ id, pedido_id, kind, filename, content_type, size_bytes, uploaded_at, review_*, url }] }
    """
    try:
        with _get_conn() as con, con.cursor() as cur:
            cur.execute(SQL_FILES_BY_PEDIDO, (pedido_id,))
            rows = cur.fetchall() or []

        items = [{
            "id": r["id"],
            "pedido_id": r["pedido_id"],
            "kind": r["kind"],
            "filename": r["filename"],
            "content_type": r["content_type"],
            "size_bytes": r["size_bytes"],
            "uploaded_at": _iso(r["uploaded_at"]),
            "review_status": r["review_status"],
            "review_notes": r["review_notes"],
            "reviewed_by": r["reviewed_by"],
            "reviewed_at": _iso(r["reviewed_at"]),
            "url": r["url"],
        } for r in rows]

        return jsonable_encoder({"items": items})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"v_ui_pedido_archivos_error: {e}")
