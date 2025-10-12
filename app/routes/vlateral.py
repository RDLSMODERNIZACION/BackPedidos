# app/routes/vlateral.py
# Endpoints de lectura para las vistas:
#   - public.v_pedido_detalle   → /ui/pedidos/{pedido_id:int}/full
#   - public.v_pedido_overview  → /ui/pedidos/overview
#
# Listo para pegar en tu proyecto. No altera tablas.
# Usa psycopg (v3). Si ya tenés un pool global, podés adaptar la función _get_conn().
# ------------------------------------------------------------------------------

from fastapi import APIRouter, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from typing import Optional, Literal
import os
import psycopg
from psycopg.rows import dict_row

router = APIRouter(prefix="/ui", tags=["ui"])

# ---------- DB helpers ----------

def _db_url() -> str:
    # Prioridad a SUPABASE_DB_URL (tu env actual), fallback a DATABASE_URL
    url = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("Falta SUPABASE_DB_URL/DATABASE_URL en el entorno")
    return url

def _get_conn():
    # Conexión corta por request. Si tenés pool, reemplazá por tu getter.
    return psycopg.connect(_db_url(), row_factory=dict_row)

# ---------- SQL ----------

SQL_DETALLE = """
SELECT *
FROM public.v_pedido_detalle
WHERE id = %s
"""

SQL_DETALLE_BY_NUMERO = """
SELECT *
FROM public.v_pedido_detalle
WHERE numero = %s
"""

# Filtros: estado, secretaria_id y búsqueda libre (q) por numero/solicitante/secretaria/modulo
# Ordenamiento simple por updated_at desc (default) o created_at desc
SQL_OVERVIEW_BASE = """
SELECT *
FROM public.v_pedido_overview
WHERE 1=1
"""

SQL_OVERVIEW_ORDER = {
    "updated_desc": "ORDER BY updated_at DESC",
    "created_desc": "ORDER BY created_at DESC",
    "total_desc":   "ORDER BY total DESC NULLS LAST",
    "total_asc":    "ORDER BY total ASC NULLS LAST",
}

# ---------- Endpoints ----------

@router.get("/pedidos/{pedido_id:int}/full")
def get_pedido_full(
    pedido_id: int,
):
    """
    Devuelve un snapshot completo de un pedido (v_pedido_detalle).
    """
    with _get_conn() as con, con.cursor() as cur:
        cur.execute(SQL_DETALLE, (pedido_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Pedido {pedido_id} no encontrado")
        return jsonable_encoder(row)


@router.get("/pedidos/full-by-numero")
def get_pedido_full_by_numero(
    numero: str = Query(..., description="Ej: EXP-2025-0042"),
):
    """
    Variante por número de expediente (v_pedido_detalle).
    """
    with _get_conn() as con, con.cursor() as cur:
        cur.execute(SQL_DETALLE_BY_NUMERO, (numero,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Pedido {numero} no encontrado")
        return jsonable_encoder(row)


@router.get("/pedidos/overview")
def get_pedidos_overview(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    estado: Optional[
        Literal["borrador", "enviado", "en_revision", "aprobado", "rechazado", "en_proceso", "area_pago", "cerrado"]
    ] = Query(None),
    secretaria_id: Optional[int] = Query(None),
    q: Optional[str] = Query(
        None,
        description="Búsqueda por numero/solicitante/secretaria/modulo (ILIKE)"
    ),
    order: Optional[
        Literal["updated_desc", "created_desc", "total_desc", "total_asc"]
    ] = Query("updated_desc"),
):
    """
    Listado liviano desde v_pedido_overview con filtros básicos y paginación.
    """
    params = []
    sql = [SQL_OVERVIEW_BASE]

    if estado:
        sql.append("AND estado = %s")
        params.append(estado)

    if secretaria_id:
        sql.append("AND secretaria_id = %s")
        params.append(secretaria_id)

    if q:
        # Busca en numero, solicitante, secretaria, modulo (case-insensitive)
        sql.append("""
            AND (
                numero ILIKE %s
                OR COALESCE(solicitante,'') ILIKE %s
                OR COALESCE(secretaria,'') ILIKE %s
                OR COALESCE(modulo,'') ILIKE %s
            )
        """)
        like = f"%{q}%"
        params.extend([like, like, like, like])

    # Orden
    sql.append(SQL_OVERVIEW_ORDER.get(order or "updated_desc", SQL_OVERVIEW_ORDER["updated_desc"]))

    # Paginación
    sql.append("LIMIT %s OFFSET %s")
    params.extend([limit, offset])

    final_sql = "\n".join(sql)

    with _get_conn() as con, con.cursor() as cur:
        cur.execute(final_sql, tuple(params))
        items = cur.fetchall()

        # Conteo total aproximado: si necesitás exacto, podés agregar un COUNT(*) en otra consulta
        # o crear una materialized view. Aquí devolvemos solo page items.
        return jsonable_encoder({
            "items": items,
            "limit": limit,
            "offset": offset,
            "order": order,
            "filters": {
                "estado": estado,
                "secretaria_id": secretaria_id,
                "q": q,
            }
        })


# ---------- Aliases anti-colisión (útiles si hay otros routers con /ui/pedidos/{pedido_id}) ----------

@router.get("/pedidos/_/overview")
def get_pedidos_overview_alias(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    estado: Optional[
        Literal["borrador", "enviado", "en_revision", "aprobado", "rechazado", "en_proceso", "area_pago", "cerrado"]
    ] = Query(None),
    secretaria_id: Optional[int] = Query(None),
    q: Optional[str] = Query(None, description="Búsqueda por numero/solicitante/secretaria/modulo (ILIKE)"),
    order: Optional[Literal["updated_desc", "created_desc", "total_desc", "total_asc"]] = Query("updated_desc"),
):
    # Reutiliza la lógica del endpoint principal
    return get_pedidos_overview(limit, offset, estado, secretaria_id, q, order)


@router.get("/pedidos/_/full-by-numero")
def get_pedido_full_by_numero_alias(
    numero: str = Query(..., description="Ej: EXP-2025-0042"),
):
    # Reutiliza la lógica del endpoint principal
    return get_pedido_full_by_numero(numero)
