# app/routes/vlateral.py
# Endpoints de lectura para las vistas:
#   - public.v_pedido_detalle   → /ui/pedidos/{pedido_id:int}/full
#   - public.v_pedido_overview  → /ui/pedidos/overview
#
# Añadido:
#   - /ui/pedidos/{pedido_id:int}/modulos       (módulos con filas reales)
#   - /ui/pedidos/{pedido_id:int}/timeline      (fechas por etapa + timeline rica)
#   - /ui/pedidos/{pedido_id:int}/etapas        (solo fechas por etapa)
#   - Aliases anti-colisión: /ui/pedidos/_/overview, /ui/pedidos/_/full-by-numero
# ------------------------------------------------------------------------------

from fastapi import APIRouter, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from typing import Optional, Literal, Dict, Any, List, Tuple
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

# ---------- Config módulos (según tu esquema real) ----------

MODULE_CANDIDATES: Dict[str, List[str]] = {
    "servicios":   ["public.pedido_servicios"],
    "alquiler":    ["public.pedido_alquiler"],
    "reparacion":  ["public.pedido_reparacion"],
    "adquisicion": ["public.pedido_adquisicion"],  # items en public.pedido_adquisicion_item
    # si más adelante agregás otros, sumalos acá
}

def _regclass_exists(con, fqname: str) -> bool:
    with con.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (fqname,))
        row = cur.fetchone()
        return bool(row and row.get("ok"))

def _rows_for_table(con, fqname: str, pedido_id: int) -> List[Dict[str, Any]]:
    sql = f"SELECT * FROM {fqname} WHERE pedido_id = %s"
    with con.cursor() as cur:
        cur.execute(sql, (pedido_id,))
        return cur.fetchall() or []

def _adquisicion_items(con, pedido_id: int) -> List[Dict[str, Any]]:
    """ Devuelve items de adquisición (si la tabla existe). """
    if not _regclass_exists(con, "public.pedido_adquisicion_item"):
        return []
    with con.cursor() as cur:
        cur.execute("SELECT * FROM public.pedido_adquisicion_item WHERE pedido_id = %s ORDER BY id", (pedido_id,))
        return cur.fetchall() or []

def _scan_modules_for_pedido(con, pedido_id: int) -> Dict[str, Any]:
    """
    Recorre todas las tablas candidatas por módulo y devuelve:
      { modulo -> { 'table': fqname, 'rows': [...] } }
    Solo incluye módulos con filas para ese pedido.
    """
    found: Dict[str, Any] = {}
    for modulo, tables in MODULE_CANDIDATES.items():
        for fq in tables:
            if not _regclass_exists(con, fq):
                continue
            rows = _rows_for_table(con, fq, pedido_id)
            if rows:
                # Enriquecemos adquisición con sus items
                if modulo == "adquisicion":
                    items = _adquisicion_items(con, pedido_id)
                    # si es una sola fila de cabecera, adjuntamos items como subcampo
                    if len(rows) == 1:
                        r0 = dict(rows[0])
                        r0["items"] = items
                        rows = [r0]
                    else:
                        # varias filas (no debería), devolvemos items aparte
                        rows = [*rows, {"_items": items}]
                found[modulo] = {"table": fq, "rows": rows}
                break  # si una de las variantes tuvo filas, no seguimos probando ese módulo
    return found

def _attach_module_payload(con, base_row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Adjunta modulo/modulo_payload si encuentra exactamente un módulo con filas.
    Si encuentra varios, adjunta modulos_payload con todos y, por compat,
    setea modulo/modulo_payload al primero (orden alfabético).
    """
    if not base_row or not base_row.get("id"):
        return base_row

    # Si ya viene desde la vista, respetamos
    if base_row.get("modulo_payload"):
        return base_row

    pedido_id = int(base_row["id"])
    found = _scan_modules_for_pedido(con, pedido_id)

    if not found:
        return base_row

    if len(found) == 1:
        (modulo_key, info), = found.items()
        rows = info["rows"]
        base_row["modulo"] = base_row.get("modulo") or modulo_key
        base_row["modulo_payload"] = rows[0] if len(rows) == 1 else rows
        base_row["modulo_table"] = info["table"]
        return base_row

    base_row["modulos_payload"] = {k: {"table": v["table"], "rows": v["rows"]} for k, v in found.items()}
    first_key = sorted(found.keys())[0]
    base_row["modulo"] = base_row.get("modulo") or first_key
    first_rows = found[first_key]["rows"]
    base_row["modulo_payload"] = first_rows[0] if len(first_rows) == 1 else first_rows
    base_row["modulo_table"] = found[first_key]["table"]
    return base_row

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

# Vistas de timeline / etapas
SQL_ETAPAS = "SELECT * FROM public.v_pedido_etapas WHERE pedido_id = %s"
SQL_TL     = "SELECT timeline FROM public.v_pedido_timeline WHERE pedido_id = %s"

# ---------- Endpoints ----------

@router.get("/pedidos/{pedido_id:int}/full")
def get_pedido_full(pedido_id: int):
    """
    Snapshot completo (v_pedido_detalle) + módulos reales del esquema.
    """
    with _get_conn() as con, con.cursor() as cur:
        cur.execute(SQL_DETALLE, (pedido_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Pedido {pedido_id} no encontrado")
        enriched = _attach_module_payload(con, dict(row))
        return jsonable_encoder(enriched)

@router.get("/pedidos/full-by-numero")
def get_pedido_full_by_numero(
    numero: str = Query(..., description="Ej: EXP-2025-0042"),
):
    """
    Variante por número (v_pedido_detalle) + módulos reales.
    """
    with _get_conn() as con, con.cursor() as cur:
        cur.execute(SQL_DETALLE_BY_NUMERO, (numero,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Pedido {numero} no encontrado")
        enriched = _attach_module_payload(con, dict(row))
        return jsonable_encoder(enriched)

@router.get("/pedidos/overview")
def get_pedidos_overview(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    estado: Optional[
        Literal["borrador", "enviado", "en_revision", "aprobado", "rechazado", "en_proceso", "area_pago", "cerrado"]
    ] = Query(None),
    secretaria_id: Optional[int] = Query(None),
    q: Optional[str] = Query(None, description="Búsqueda por numero/solicitante/secretaria/modulo (ILIKE)"),
    order: Optional[Literal["updated_desc", "created_desc", "total_desc", "total_asc"]] = Query("updated_desc"),
):
    """
    Listado liviano desde v_pedido_overview (sin hidratar módulos para mantenerlo rápido).
    """
    params: List[Any] = []
    sql = [SQL_OVERVIEW_BASE]

    if estado:
        sql.append("AND estado = %s")
        params.append(estado)
    if secretaria_id:
        sql.append("AND secretaria_id = %s")
        params.append(secretaria_id)
    if q:
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

    sql.append(SQL_OVERVIEW_ORDER.get(order or "updated_desc", SQL_OVERVIEW_ORDER["updated_desc"]))
    sql.append("LIMIT %s OFFSET %s")
    params.extend([limit, offset])

    with _get_conn() as con, con.cursor() as cur:
        cur.execute("\n".join(sql), tuple(params))
        items = cur.fetchall()
        return jsonable_encoder({
            "items": items,
            "limit": limit,
            "offset": offset,
            "order": order,
            "filters": {"estado": estado, "secretaria_id": secretaria_id, "q": q},
        })

# ---------- NUEVO: ver todos los módulos con filas ----------

@router.get("/pedidos/{pedido_id:int}/modulos")
def get_pedido_modulos(pedido_id: int):
    """
    Devuelve todos los módulos que tengan filas para este pedido:
    {
      "found": {
        "servicios":   { "table": "public.pedido_servicios",   "rows": [...] },
        "alquiler":    { "table": "public.pedido_alquiler",    "rows": [...] },
        "reparacion":  { "table": "public.pedido_reparacion",  "rows": [...] },
        "adquisicion": { "table": "public.pedido_adquisicion", "rows": [ {.., items:[...]} ] }
      }
    }
    """
    with _get_conn() as con:
        found = _scan_modules_for_pedido(con, pedido_id)
        return jsonable_encoder({"found": found})

# ---------- NUEVO: etapas + timeline ----------

@router.get("/pedidos/{pedido_id:int}/etapas")
def get_pedido_etapas(pedido_id: int):
    """
    Devuelve las fechas por etapa (v_pedido_etapas) para un pedido.
    """
    with _get_conn() as con, con.cursor() as cur:
        cur.execute(SQL_ETAPAS, (pedido_id,))
        row = cur.fetchone()
        return jsonable_encoder(row or {})

@router.get("/pedidos/{pedido_id:int}/timeline")
def get_pedido_timeline(pedido_id: int):
    """
    Devuelve:
    {
      "etapas":   {creado_at, enviado_at, ..., expediente_2_at},
      "timeline": [{kind, at, ...}, ...]
    }
    Requiere vistas: v_pedido_etapas, v_pedido_timeline
    """
    with _get_conn() as con, con.cursor() as cur:
        cur.execute(SQL_ETAPAS, (pedido_id,))
        etapas = cur.fetchone()

        cur.execute(SQL_TL, (pedido_id,))
        tl_row = cur.fetchone()
        timeline = (tl_row or {}).get("timeline")

        return jsonable_encoder({"etapas": etapas, "timeline": timeline or []})

# ---------- Aliases anti-colisión ----------

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
    return get_pedidos_overview(limit, offset, estado, secretaria_id, q, order)

@router.get("/pedidos/_/full-by-numero")
def get_pedido_full_by_numero_alias(
    numero: str = Query(..., description="Ej: EXP-2025-0042"),
):
    return get_pedido_full_by_numero(numero)
