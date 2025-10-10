# routes/ui.py
from fastapi import APIRouter, HTTPException, Query
from typing import Optional, Literal, Dict, Any, List
from psycopg.rows import dict_row
from app.db import get_conn

router = APIRouter(prefix="/ui", tags=["ui"])

SortParam = Literal[
    "updated_at_desc", "updated_at_asc",
    "created_at_desc", "created_at_asc",
    "total_desc", "total_asc"
]

@router.get("/pedidos/list")
def ui_pedidos_list(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    q: Optional[str] = Query(None, description="Búsqueda en id_tramite, secretaria, solicitante, módulo"),
    estado: Optional[str] = Query(None, description="Filtra por estado exacto (borrador/enviado/en_revision/aprobado/rechazado/cerrado)"),
    modulo: Optional[str] = Query(None, description="Filtra por módulo/ámbito (ILIKE)"),
    sort: SortParam = Query("updated_at_desc")
) -> Dict[str, Any]:
    """
    Lista de pedidos para la grilla del front:
    - id, id_tramite (EXP-YYYY-####), modulo (o ámbito), secretaria, solicitante, estado, total, creado
    - Paginación: limit + offset
    - Filtros: q (texto), estado (exacto), modulo (ILIKE)
    - Orden: updated/created/total asc/desc
    """
    # Mapeo de orden
    sort_sql = {
        "updated_at_desc": "ORDER BY updated_at DESC",
        "updated_at_asc":  "ORDER BY updated_at ASC",
        "created_at_desc": "ORDER BY creado DESC",
        "created_at_asc":  "ORDER BY creado ASC",
        "total_desc":      "ORDER BY total DESC NULLS LAST",
        "total_asc":       "ORDER BY total ASC NULLS FIRST",
    }[sort]

    # WHERE dinámico
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
            id_tramite ILIKE %(q)s
            OR secretaria ILIKE %(q)s
            OR COALESCE(solicitante,'') ILIKE %(q)s
            OR modulo ILIKE %(q)s
        )""")
        params["q"] = f"%{q}%"

    where_sql = "WHERE " + " AND ".join(wh) if wh else ""

    # SQL base sobre la vista
    base = f"""
      SELECT id, id_tramite, modulo, secretaria, solicitante, estado, total, creado
      FROM public.ui_pedidos_listado
      {where_sql}
    """

    # Conteo total para paginación
    sql_count = f"SELECT COUNT(*) AS count FROM ({base}) t"
    sql_page  = f"{base} {sort_sql} LIMIT %(limit)s OFFSET %(offset)s"

    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql_count, params)
            count = cur.fetchone()["count"]

            cur.execute(sql_page, params)
            items = cur.fetchall()

        return {
            "items": items,
            "count": count,
            "limit": limit,
            "offset": offset,
            "sort": sort,
            "filters": {"q": q, "estado": estado, "modulo": modulo},
        }
    except Exception as e:
        # Si la vista no existe o hay error SQL
        raise HTTPException(status_code=500, detail=f"Error listando pedidos: {e}")
