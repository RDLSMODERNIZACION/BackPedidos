# app/routes/vlateral.py
# Endpoints de lectura para las vistas:
#   - public.v_pedido_detalle   → /ui/pedidos/{pedido_id:int}/full
#   - public.v_pedido_overview  → /ui/pedidos/overview
#
# Añadido:
#   - Hidratación automática de modulo/modulo_payload
#   - GET /ui/pedidos/{pedido_id:int}/modulo  (solo módulo)
#
# Listo para pegar. No altera tablas, solo consulta.

from fastapi import APIRouter, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from typing import Optional, Literal, Tuple, List, Dict, Any
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

# ---------- Módulos: detección de tabla ----------

# Candidatos por módulo. Ajustá nombres si tus tablas difieren.
MODULE_CANDIDATES: Dict[str, List[str]] = {
    "servicios": [
        "public.pedidos_servicios", "public.pedido_servicios", "public.servicios_pedido"
    ],
    "alquiler": [
        "public.pedidos_alquiler", "public.pedido_alquiler"
    ],
    "adquisicion": [
        "public.pedidos_adquisicion", "public.pedidos_adquisiciones", "public.adquisicion_pedido"
    ],
    "reparacion": [
        "public.pedidos_reparacion", "public.pedidos_reparaciones", "public.reparacion_pedido"
    ],
    "obras": [
        "public.pedidos_obras", "public.pedido_obras", "public.pedidos_obra"
    ],
    "mantenimiento_escuelas": [
        "public.pedidos_mantenimiento_escuelas",
        "public.pedidos_mant_escuela",
        "public.pedidos_mantescuela",
    ],
}

def _to_regclass_exists(con, fqname: str) -> bool:
    """Devuelve True si to_regclass(fqname) no es NULL."""
    with con.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL AS exists", (fqname,))
        row = cur.fetchone()
        return bool(row and row.get("exists"))

def _resolve_module_table(con, modulo: Optional[str]) -> Optional[Tuple[str, str]]:
    """
    Dado un modulo (p.ej. 'servicios'), encuentra la primera tabla existente.
    Retorna (modulo_normalizado, table_name) o None si no encuentra.
    """
    if not modulo:
        return None
    key = modulo.strip().lower()
    candidates = MODULE_CANDIDATES.get(key, [])
    for fq in candidates:
        if _to_regclass_exists(con, fq):
            return key, fq
    return None

def _probe_any_existing_module_table(con) -> Optional[Tuple[str, str]]:
    """
    Si no viene 'modulo' en la vista, buscamos en TODAS las tablas candidatas
    y devolvemos la primera que exista (encontrada por orden del dict).
    """
    for modulo, candidates in MODULE_CANDIDATES.items():
        for fq in candidates:
            if _to_regclass_exists(con, fq):
                return modulo, fq
    return None

def _fetch_module_rows(con, table_fqname: str, pedido_id: int) -> List[Dict[str, Any]]:
    """
    Devuelve filas del módulo para el pedido dado. Asume columna 'pedido_id'.
    """
    sql = f"SELECT * FROM {table_fqname} WHERE pedido_id = %s"
    with con.cursor() as cur:
        cur.execute(sql, (pedido_id,))
        rows = cur.fetchall()
        return rows or []

def _hydrate_modulo(con, base_row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Enriquecer base_row agregando 'modulo' (si falta) y 'modulo_payload' con
    el/los registros del módulo correspondiente.
    """
    if not base_row:
        return base_row

    modulo = (base_row.get("modulo") or "") or None
    pedido_id = base_row.get("id")
    if not pedido_id:
        return base_row

    # Si ya vino payload desde la vista y no es nulo, lo respetamos
    if base_row.get("modulo_payload"):
        return base_row

    # Intentamos resolver por 'modulo' si existe; si no, probamos cualquiera
    resolved = _resolve_module_table(con, modulo) or _probe_any_existing_module_table(con)
    if not resolved:
        return base_row

    modulo_key, table_fqname = resolved
    rows = _fetch_module_rows(con, table_fqname, pedido_id)

    # Si encontramos filas, adjuntamos
    if rows:
        # si antes venía null, asentamos módulo detectado
        if not modulo:
            base_row["modulo"] = modulo_key
        # payload: si una sola fila, devolvemos objeto; si varias, lista
        base_row["modulo_payload"] = rows[0] if len(rows) == 1 else rows
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

# ---------- Endpoints ----------

@router.get("/pedidos/{pedido_id:int}/full")
def get_pedido_full(pedido_id: int):
    """
    Devuelve un snapshot completo de un pedido (v_pedido_detalle) + modulo_payload hidratado.
    """
    with _get_conn() as con, con.cursor() as cur:
        cur.execute(SQL_DETALLE, (pedido_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Pedido {pedido_id} no encontrado")

        row = _hydrate_modulo(con, dict(row))
        return jsonable_encoder(row)

@router.get("/pedidos/full-by-numero")
def get_pedido_full_by_numero(
    numero: str = Query(..., description="Ej: EXP-2025-0042"),
):
    """
    Variante por número de expediente (v_pedido_detalle) + modulo_payload hidratado.
    """
    with _get_conn() as con, con.cursor() as cur:
        cur.execute(SQL_DETALLE_BY_NUMERO, (numero,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Pedido {numero} no encontrado")

        row = _hydrate_modulo(con, dict(row))
        return jsonable_encoder(row)

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
    Listado liviano desde v_pedido_overview con filtros básicos y paginación.
    (No hidrata módulo acá para mantenerlo rápido.)
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

    final_sql = "\n".join(sql)

    with _get_conn() as con, con.cursor() as cur:
        cur.execute(final_sql, tuple(params))
        items = cur.fetchall()
        return jsonable_encoder({
            "items": items,
            "limit": limit,
            "offset": offset,
            "order": order,
            "filters": {"estado": estado, "secretaria_id": secretaria_id, "q": q},
        })

# ---------- Endpoint nuevo: solo módulo ----------

@router.get("/pedidos/{pedido_id:int}/modulo")
def get_pedido_modulo(pedido_id: int):
    """
    Devuelve únicamente {'modulo': <str>, 'payload': <obj|lista>, 'table': <fqname>} para un pedido.
    """
    with _get_conn() as con, con.cursor() as cur:
        cur.execute(SQL_DETALLE, (pedido_id,))
        base = cur.fetchone()
        if not base:
            raise HTTPException(status_code=404, detail=f"Pedido {pedido_id} no encontrado")

        modulo = (base.get("modulo") or "") or None
        resolved = _resolve_module_table(con, modulo) or _probe_any_existing_module_table(con)
        if not resolved:
            return {"modulo": modulo, "payload": None, "table": None}

        modulo_key, table_fqname = resolved
        rows = _fetch_module_rows(con, table_fqname, pedido_id)
        payload = rows[0] if len(rows) == 1 else rows
        return jsonable_encoder({"modulo": modulo_key or modulo, "payload": payload, "table": table_fqname})

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
