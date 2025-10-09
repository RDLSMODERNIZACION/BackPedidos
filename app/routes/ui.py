# ============================
# v_pedidos_list — listado y detalle
# ============================
from typing import List, Optional
from fastapi import Query, HTTPException
from psycopg.rows import dict_row
from psycopg import Error as PsycopgError
from app.db import get_conn

@router.get("/pedidos/list", response_model=List[dict])
def list_pedidos(
    q: Optional[str] = Query(None, description="Busca en numero, secretaria o solicitante (ILIKE)"),
    modulo: Optional[str] = Query(None, description="Filtro exacto por modulo (ej: servicios)"),
    estado: Optional[str] = Query(None, description="Filtro exacto por estado (ej: enviado)"),
    secretaria: Optional[str] = Query(None, description="Filtro exacto por secretaria"),
    created_by: Optional[str] = Query(None, description="user_id creador"),
    fecha_desde: Optional[str] = Query(None, description="YYYY-MM-DD (inclusive)"),
    fecha_hasta: Optional[str] = Query(None, description="YYYY-MM-DD (inclusive)"),
    min_total: Optional[float] = Query(None, ge=0),
    max_total: Optional[float] = Query(None, ge=0),
    order: str = Query(
        "created_at_desc",
        pattern="^(created_at_(asc|desc)|creado_(asc|desc)|fecha_pedido_(asc|desc)|numero_(asc|desc)|total_(asc|desc)|id_(asc|desc))$",
    ),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    base_sql = """
        SELECT
          id, numero, modulo, modulo_name, estado, estado_label, secretaria,
          creado, created_by, solicitante, total, fecha_pedido,
          presupuesto_estimado, created_at, updated_at
        FROM public.v_pedidos_list
    """
    where, params = [], []

    if q:
        where.append("(numero ILIKE %s OR secretaria ILIKE %s OR solicitante ILIKE %s)")
        like = f"%{q}%"
        params.extend([like, like, like])

    if modulo:
        where.append("modulo = %s"); params.append(modulo)
    if estado:
        where.append("estado = %s"); params.append(estado)
    if secretaria:
        where.append("secretaria = %s"); params.append(secretaria)
    if created_by:
        where.append("created_by = %s"); params.append(created_by)
    if fecha_desde:
        where.append("fecha_pedido >= %s::date"); params.append(fecha_desde)
    if fecha_hasta:
        where.append("fecha_pedido <= %s::date"); params.append(fecha_hasta)
    if min_total is not None:
        where.append("total >= %s::numeric"); params.append(min_total)
    if max_total is not None:
        where.append("total <= %s::numeric"); params.append(max_total)

    if where:
        base_sql += " WHERE " + " AND ".join(where)

    order_clause = {
        "created_at_asc":   " ORDER BY created_at ASC, id ASC ",
        "created_at_desc":  " ORDER BY created_at DESC, id DESC ",
        "creado_asc":       " ORDER BY creado ASC, id ASC ",
        "creado_desc":      " ORDER BY creado DESC, id DESC ",
        "fecha_pedido_asc": " ORDER BY fecha_pedido ASC, id ASC ",
        "fecha_pedido_desc":" ORDER BY fecha_pedido DESC, id DESC ",
        "numero_asc":       " ORDER BY numero ASC ",
        "numero_desc":      " ORDER BY numero DESC ",
        "total_asc":        " ORDER BY total ASC NULLS LAST, id ASC ",
        "total_desc":       " ORDER BY total DESC NULLS LAST, id DESC ",
        "id_asc":           " ORDER BY id ASC ",
        "id_desc":          " ORDER BY id DESC ",
    }[order]

    sql = base_sql + order_clause + " LIMIT %s OFFSET %s "
    params.extend([limit, offset])

    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except PsycopgError as e:
        raise HTTPException(status_code=500, detail=f"DB error v_pedidos_list: {e.__class__.__name__}: {str(e).strip()}")

@router.get("/pedidos/list/{id}", response_model=dict)
def get_pedido_list_item(id: int):
    sql = """
        SELECT
          id, numero, modulo, modulo_name, estado, estado_label, secretaria,
          creado, created_by, solicitante, total, fecha_pedido,
          presupuesto_estimado, created_at, updated_at
        FROM public.v_pedidos_list
        WHERE id = %s
        LIMIT 1;
    """
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Pedido no encontrado en v_pedidos_list")
            return row
    except PsycopgError as e:
        raise HTTPException(status_code=500, detail=f"DB error v_pedidos_list: {e.__class__.__name__}: {str(e).strip()}")
