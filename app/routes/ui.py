# app/routers/ui.py
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from psycopg.rows import dict_row

from app.db import get_conn

router = APIRouter(prefix="/ui", tags=["ui"])

# ============================================================
# ARCHIVOS (v_ui_pedido_archivos)
# ============================================================

@router.get("/pedido-archivos", response_model=List[dict])
async def list_pedido_archivos(
    pedido_id: Optional[int] = Query(None, description="Filtra por pedido_id"),
    created_by: Optional[str] = Query(None, description="Filtra por user_id creador"),
    q: Optional[str] = Query(None, description="Búsqueda por file_name ILIKE"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    order: str = Query("created_at_desc", pattern="^(created_at_(asc|desc)|id_(asc|desc))$")
):
    """
    Lista archivos de la vista v_ui_pedido_archivos SIN traer el campo bytes.
    Devuelve metadatos + bytes_len (tamaño del binario) para referencia.
    """
    base_sql = """
        SELECT
            id,
            pedido_id,
            file_name,
            content_type,
            octet_length(bytes) AS bytes_len,
            storage_path,
            created_at,
            created_by,
            created_by_name
        FROM public.v_ui_pedido_archivos
    """

    where = []
    params = []

    if pedido_id is not None:
        where.append("pedido_id = %s")
        params.append(pedido_id)

    if created_by is not None:
        where.append("created_by = %s")
        params.append(created_by)

    if q:
        where.append("file_name ILIKE %s")
        params.append(f"%{q}%")

    if where:
        base_sql += " WHERE " + " AND ".join(where)

    order_clause = {
        "created_at_asc": " ORDER BY created_at ASC, id ASC ",
        "created_at_desc": " ORDER BY created_at DESC, id DESC ",
        "id_asc": " ORDER BY id ASC ",
        "id_desc": " ORDER BY id DESC ",
    }[order]

    paging = " LIMIT %s OFFSET %s "
    params.extend([limit, offset])

    sql = base_sql + order_clause + paging

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        return rows


@router.get("/pedido-archivos/{id}/download")
async def download_pedido_archivo(id: int):
    """
    Descarga el archivo por id desde la vista v_ui_pedido_archivos.
    Usa content_type y file_name para armar los headers.
    """
    sql = """
        SELECT file_name, content_type, bytes
        FROM public.v_ui_pedido_archivos
        WHERE id = %s
        LIMIT 1;
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (id,))
        row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")

    if row["bytes"] is None:
        # Si manejás storage externo con storage_path y no guardás bytes en DB,
        # acá podrías implementar la lectura desde el storage. Por ahora 404.
        raise HTTPException(status_code=404, detail="El archivo no tiene contenido almacenado en 'bytes'")

    file_name = row["file_name"] or f"archivo_{id}"
    content_type = row["content_type"] or "application/octet-stream"
    data = bytes(row["bytes"])  # psycopg devuelve memoryview -> lo convertimos

    headers = {
        # Soporta nombres UTF-8 correctamente
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(file_name)}"
    }
    return Response(content=data, media_type=content_type, headers=headers)


# ============================================================
# EVENTOS (v_ui_pedido_eventos)
# ============================================================

@router.get("/pedido-eventos", response_model=List[dict])
async def list_pedido_eventos(
    pedido_id: Optional[int] = Query(None, description="Filtra por pedido_id"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    order_ts: str = Query("desc", pattern="^(asc|desc)$")
):
    """
    Lista eventos desde v_ui_pedido_eventos.
    """
    base_sql = """
        SELECT
            pedido_id,
            ts,
            accion,
            accion_label,
            actor,
            comentario,
            meta
        FROM public.v_ui_pedido_eventos
    """

    where = []
    params = []
    if pedido_id is not None:
        where.append("pedido_id = %s")
        params.append(pedido_id)

    if where:
        base_sql += " WHERE " + " AND ".join(where)

    order_clause = " ORDER BY ts ASC " if order_ts == "asc" else " ORDER BY ts DESC "
    paging = " LIMIT %s OFFSET %s "
    params.extend([limit, offset])

    sql = base_sql + order_clause + paging

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        return rows


@router.get("/pedidos/{pedido_id}/eventos", response_model=List[dict])
async def list_pedido_eventos_por_pedido(
    pedido_id: int,
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    order_ts: str = Query("asc", pattern="^(asc|desc)$")
):
    """
    Azúcar sintáctica: eventos de un pedido específico (orden por defecto ascendente).
    """
    order_clause = " ORDER BY ts ASC " if order_ts == "asc" else " ORDER BY ts DESC "
    sql = f"""
        SELECT
            pedido_id,
            ts,
            accion,
            accion_label,
            actor,
            comentario,
            meta
        FROM public.v_ui_pedido_eventos
        WHERE pedido_id = %s
        {order_clause}
        LIMIT %s OFFSET %s;
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (pedido_id, limit, offset))
        rows = cur.fetchall()
        return rows
