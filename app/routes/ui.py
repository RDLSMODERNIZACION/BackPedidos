# routes/ui.py
from fastapi import APIRouter, HTTPException, Query
from typing import Optional, Literal, Dict, Any, List
from psycopg.rows import dict_row
from psycopg.errors import OperationalError, DatabaseError
import time
from app.db import get_conn

router = APIRouter(prefix="/ui", tags=["ui"])

SortParam = Literal[
    "updated_at_desc", "updated_at_asc",
    "created_at_desc", "created_at_asc",
    "total_desc", "total_asc"
]

def _sort_sql(kind: SortParam) -> str:
    return {
        "updated_at_desc": "ORDER BY updated_at DESC",
        "updated_at_asc":  "ORDER BY updated_at ASC",
        "created_at_desc": "ORDER BY creado DESC",
        "created_at_asc":  "ORDER BY creado ASC",
        "total_desc":      "ORDER BY total DESC NULLS LAST",
        "total_asc":       "ORDER BY total ASC NULLS FIRST",
    }[kind]

@router.get("/pedidos/list")
def ui_pedidos_list(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    q: Optional[str] = Query(None),
    estado: Optional[str] = Query(None),
    modulo: Optional[str] = Query(None),
    sort: SortParam = Query("updated_at_desc"),
) -> Dict[str, Any]:
    # WHERE dinámico (solo columnas que EXISTEN en tu vista actual)
    wh: List[str] = []
    params: Dict[str, Any] = {"limit": limit, "offset": offset}

    if estado:
        wh.append("estado = %(estado)s")
        params["estado"] = estado

    if modulo:
        wh.append("modulo ILIKE %(modulo)s")
        params["modulo"] = f"%{modulo}%"

    if q:
        wh.append("""(
           id_tramite ILIKE %(q)s OR
           modulo ILIKE %(q)s OR
           secretaria ILIKE %(q)s OR
           COALESCE(solicitante,'') ILIKE %(q)s
        )""")
        params["q"] = f"%{q}%"

    where_sql = "WHERE " + " AND ".join(wh) if wh else ""
    order_sql = _sort_sql(sort)

    # UNA SOLA QUERY (datos + total con ventana) usando SOLO columnas existentes
    sql = f"""
      SELECT
        id,
        id_tramite,
        modulo,
        secretaria,
        solicitante,
        estado,
        total,
        creado,
        updated_at,
        COUNT(*) OVER() AS _total_count
      FROM public.ui_pedidos_listado
      {where_sql}
      {order_sql}
      LIMIT %(limit)s OFFSET %(offset)s
    """

    # Retry corto por si la conexión se cerró (Render + libpq)
    for attempt in (1, 2):
        try:
            with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
                total = rows[0]["_total_count"] if rows else 0
                for r in rows:
                    r.pop("_total_count", None)
                return {
                    "items": rows,
                    "count": total,
                    "limit": limit,
                    "offset": offset,
                    "sort": sort,
                    "filters": {"q": q, "estado": estado, "modulo": modulo},
                }
        except (OperationalError, DatabaseError) as e:
            msg = str(e)
            if "server closed the connection unexpectedly" in msg and attempt == 1:
                time.sleep(0.3)
                continue
            raise HTTPException(status_code=500, detail=f"Error listando pedidos: {e}")
