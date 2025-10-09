# app/routes/ui.py
from typing import List, Optional, Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Response
from psycopg.rows import dict_row
from psycopg import Error as PsycopgError

from app.db import get_conn  # ajustá si tu helper vive en otro módulo

router = APIRouter(prefix="/ui", tags=["ui"])  # ✅ ESTE FALTABA

# ------------------------------------------------------------
# DEBUG 1: estado de conexión y existencia de vistas
# ------------------------------------------------------------
@router.get("/debug/db")
def debug_db():
    sql = """
    SELECT
      current_database() AS db,
      current_user       AS usr,
      to_regclass('public.v_ui_pedido_archivos') AS v_arch,
      to_regclass('public.v_ui_pedido_eventos')  AS v_evt
    """
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql)
            return cur.fetchone()
    except PsycopgError as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e.__class__.__name__}: {str(e).strip()}")

# ------------------------------------------------------------
# DEBUG 2: inspección de una vista (definición, columnas, sample)
# ------------------------------------------------------------
@router.get("/debug/view/{which}")
def debug_view(which: Literal["eventos", "archivos"]):
    viewmap = {
        "eventos": "public.v_ui_pedido_eventos",
        "archivos": "public.v_ui_pedido_archivos",
    }
    view = viewmap[which]
    schema, name = view.split(".")

    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT to_regclass(%s) AS oid", (view,))
            exists = cur.fetchone()["oid"] is not None
            if not exists:
                return {"exists": False, "view": view}

            cur.execute("""
                SELECT
                    pg_get_viewdef(%s::regclass, true) AS definition,
                    (SELECT relowner::regrole::text FROM pg_class WHERE oid = %s::regclass) AS owner
            """, (view, view))
            meta = cur.fetchone()

            cur.execute("""
                SELECT is_updatable, is_insertable_into
                FROM information_schema.views
                WHERE table_schema = %s AND table_name = %s
            """, (schema, name))
            up = cur.fetchone()

            cur.execute("""
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
            """, (schema, name))
            columns = cur.fetchall()

            cur.execute(f"SELECT count(*) AS n FROM {view}")
            n = cur.fetchone()["n"]

            cur.execute(f"SELECT * FROM {view} ORDER BY 1 DESC LIMIT 5")
            sample = cur.fetchall()

            return {
                "exists": True,
                "view": view,
                "owner": meta["owner"],
                "is_updatable": (up or {}).get("is_updatable"),
                "is_insertable_into": (up or {}).get("is_insertable_into"),
                "columns": columns,
                "count": n,
                "sample": sample,
                "definition": meta["definition"],
            }
    except PsycopgError as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e.__class__.__name__}: {str(e).strip()}")

# ------------------------------------------------------------
# v_ui_pedido_archivos — listar (sin bytes) y descargar
# ------------------------------------------------------------
@router.get("/pedido-archivos", response_model=List[dict])
async def list_pedido_archivos(
    pedido_id: Optional[int] = Query(None),
    created_by: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    order: str = Query("created_at_desc", pattern="^(created_at_(asc|desc)|id_(asc|desc))$")
):
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
    where, params = [], []
    if pedido_id is not None:
        where.append("pedido_id = %s"); params.append(pedido_id)
    if created_by is not None:
        where.append("created_by = %s"); params.append(created_by)
    if q:
        where.append("file_name ILIKE %s"); params.append(f"%{q}%")
    if where:
        base_sql += " WHERE " + " AND ".join(where)

    order_clause = {
        "created_at_asc": " ORDER BY created_at ASC, id ASC ",
        "created_at_desc": " ORDER BY created_at DESC, id DESC ",
        "id_asc": " ORDER BY id ASC ",
        "id_desc": " ORDER BY id DESC ",
    }[order]

    sql = base_sql + order_clause + " LIMIT %s OFFSET %s "
    params.extend([limit, offset])

    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except PsycopgError as e:
        raise HTTPException(status_code=500, detail=f"DB error v_ui_pedido_archivos: {e.__class__.__name__}: {str(e).strip()}")

@router.get("/pedido-archivos/{id}/download")
async def download_pedido_archivo(id: int):
    sql = """
        SELECT file_name, content_type, bytes
        FROM public.v_ui_pedido_archivos
        WHERE id = %s
        LIMIT 1;
    """
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (id,))
            row = cur.fetchone()
    except PsycopgError as e:
        raise HTTPException(status_code=500, detail=f"DB error v_ui_pedido_archivos: {e.__class__.__name__}: {str(e).strip()}")

    if not row:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    if row["bytes"] is None:
        raise HTTPException(status_code=404, detail="El archivo no tiene contenido en 'bytes' (usa storage_path)")

    file_name = row["file_name"] or f"archivo_{id}"
    content_type = row["content_type"] or "application/octet-stream"
    data = bytes(row["bytes"])  # psycopg -> memoryview
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(file_name)}"}
    return Response(content=data, media_type=content_type, headers=headers)

# ------------------------------------------------------------
# v_ui_pedido_eventos — listar
# ------------------------------------------------------------
@router.get("/pedido-eventos", response_model=List[dict])
async def list_pedido_eventos(
    pedido_id: Optional[int] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    order_ts: str = Query("desc", pattern="^(asc|desc)$")
):
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
    where, params = [], []
    if pedido_id is not None:
        where.append("pedido_id = %s"); params.append(pedido_id)
    if where:
        base_sql += " WHERE " + " AND ".join(where)

    order_clause = " ORDER BY ts ASC " if order_ts == "asc" else " ORDER BY ts DESC "
    sql = base_sql + order_clause + " LIMIT %s OFFSET %s "
    params.extend([limit, offset])

    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except PsycopgError as e:
        raise HTTPException(status_code=500, detail=f"DB error v_ui_pedido_eventos: {e.__class__.__name__}: {str(e).strip()}")

@router.get("/pedidos/{pedido_id}/eventos", response_model=List[dict])
async def list_pedido_eventos_por_pedido(
    pedido_id: int,
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    order_ts: str = Query("asc", pattern="^(asc|desc)$")
):
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
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (pedido_id, limit, offset))
            return cur.fetchall()
    except PsycopgError as e:
        raise HTTPException(status_code=500, detail=f"DB error v_ui_pedido_eventos: {e.__class__.__name__}: {str(e).strip()}")
