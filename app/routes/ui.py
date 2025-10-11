# routes/ui.py
from fastapi import APIRouter, HTTPException, Query, Header, UploadFile, File
from pydantic import BaseModel
from typing import Optional, Literal, Dict, Any, List
from psycopg.rows import dict_row
from psycopg.errors import OperationalError, DatabaseError
from decimal import Decimal
import time
import os
import shutil

from app.db import get_conn
# 🔁 importo el uploader nuevo para delegar en él (sube a Supabase y hace transiciones)
from app.routes.pedidos import upload_archivo

router = APIRouter(prefix="/ui", tags=["ui"])

FILES_DIR = os.getenv("FILES_DIR", "files")

# =========================
# Listado
# =========================

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
    """
    Listado SIN dependencia de vistas. Detecta 'modulo' por existencia en tablas
    y calcula 'total' con suma de items de adquisición (o presupuesto_estimado).
    """
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

    sql = f"""
    WITH det AS (
      SELECT
        p.id,
        p.numero AS id_tramite,
        CASE
          WHEN EXISTS (SELECT 1 FROM public.pedido_servicios   ps  WHERE ps.pedido_id = p.id) THEN 'servicios'
          WHEN EXISTS (SELECT 1 FROM public.pedido_alquiler    pa  WHERE pa.pedido_id = p.id) THEN 'alquiler'
          WHEN EXISTS (SELECT 1 FROM public.pedido_adquisicion pad WHERE pad.pedido_id = p.id) THEN 'adquisicion'
          WHEN EXISTS (SELECT 1 FROM public.pedido_reparacion  pr  WHERE pr.pedido_id = p.id) THEN 'reparacion'
          ELSE NULL
        END AS modulo,
        s.nombre AS secretaria,
        COALESCE(perf.nombre, perf.login_username) AS solicitante,
        p.estado,
        COALESCE(
          (SELECT SUM(COALESCE(ai.total, ai.cantidad * COALESCE(ai.precio_unitario, 0)))
             FROM public.pedido_adquisicion_item ai
            WHERE ai.pedido_id = p.id),
          p.presupuesto_estimado
        ) AS total,
        p.created_at AS creado,
        p.updated_at
      FROM public.pedido p
      JOIN public.secretaria s ON s.id = p.secretaria_id
      LEFT JOIN public.perfil perf ON perf.user_id = p.created_by
    )
    SELECT *
      FROM (
        SELECT det.*, COUNT(*) OVER() AS _total_count
          FROM det
      ) x
      {where_sql}
      {order_sql}
      LIMIT %(limit)s OFFSET %(offset)s
    """

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

# =========================
# Detalle (para pestaña Info)
# =========================

@router.get("/pedidos/{pedido_id}")
def ui_pedido_detalle(pedido_id: int) -> Dict[str, Any]:
    """
    Devuelve generales + ambiente + módulo del pedido.
    """
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            # Generales
            cur.execute("""
                SELECT
                  p.id, p.numero, p.estado,
                  p.fecha_pedido, p.fecha_desde, p.fecha_hasta,
                  p.presupuesto_estimado, p.observaciones,
                  p.created_at AS creado,
                  s.nombre AS secretaria,
                  perf.nombre AS solicitante
                FROM public.pedido p
                JOIN public.secretaria s ON s.id = p.secretaria_id
                LEFT JOIN public.perfil perf ON perf.user_id = p.created_by
                WHERE p.id = %s
            """, (pedido_id,))
            base = cur.fetchone()
            if not base:
                raise HTTPException(status_code=404, detail="Pedido no encontrado")

            out: Dict[str, Any] = { **base, "ambito": None, "modulo": None }

            # Ámbito
            cur.execute("SELECT tipo::text AS tipo_db FROM public.pedido_ambito WHERE pedido_id=%s", (pedido_id,))
            amb = cur.fetchone()
            tipo_db = (amb or {}).get("tipo_db", "general")

            if tipo_db == "obra":
                cur.execute("""
                    SELECT nombre_obra, ubicacion, detalle, presupuesto_obra,
                           fecha_inicio, fecha_fin, es_nueva, obra_existente_ref
                      FROM public.ambito_obra
                     WHERE pedido_id=%s
                """, (pedido_id,))
                r = cur.fetchone() or {}
                out["ambito"] = {
                    "tipo": "obra",
                    "obra": {
                        "obra_nombre": r.get("nombre_obra"),
                        "ubicacion": r.get("ubicacion"),
                        "detalle": r.get("detalle"),
                        "presupuesto_obra": r.get("presupuesto_obra"),
                        "fecha_inicio": r.get("fecha_inicio"),
                        "fecha_fin": r.get("fecha_fin"),
                        "es_nueva": r.get("es_nueva"),
                        "obra_existente_ref": r.get("obra_existente_ref"),
                    }
                }
            elif tipo_db == "mant_escuela":
                cur.execute("""
                    SELECT escuela, ubicacion, necesidad, fecha_desde, fecha_hasta, detalle
                      FROM public.ambito_mant_escuela
                     WHERE pedido_id=%s
                """, (pedido_id,))
                r = cur.fetchone() or {}
                out["ambito"] = {
                    "tipo": "mantenimientodeescuelas",
                    "escuelas": {
                        "escuela": r.get("escuela"),
                        "ubicacion": r.get("ubicacion"),
                        "necesidad": r.get("necesidad"),
                        "fecha_desde": r.get("fecha_desde"),
                        "fecha_hasta": r.get("fecha_hasta"),
                        "detalle": r.get("detalle"),
                    }
                }
            else:
                out["ambito"] = {"tipo": "ninguno"}

            # Módulo (uno por pedido)
            # Servicios
            cur.execute("""
                SELECT
                  tipo_servicio,
                  COALESCE(detalle_mantenimiento, servicio_requerido) AS detalle_mantenimiento,
                  tipo_profesional, dia_desde, dia_hasta
                  FROM public.pedido_servicios
                 WHERE pedido_id=%s
            """, (pedido_id,))
            r = cur.fetchone()
            if r:
                out["modulo"] = {"tipo": "servicios", **r}
                return out

            # Alquiler
            cur.execute("""
                SELECT categoria, uso_edificio, ubicacion_edificio,
                       uso_maquinaria, tipo_maquinaria,
                       requiere_combustible, requiere_chofer,
                       cronograma_desde, cronograma_hasta, horas_por_dia,
                       que_alquilar, detalle_uso
                  FROM public.pedido_alquiler
                 WHERE pedido_id=%s
            """, (pedido_id,))
            r = cur.fetchone()
            if r:
                out["modulo"] = {"tipo": "alquiler", **r}
                return out

            # Adquisición + items
            cur.execute("""
                SELECT proposito, modo_adquisicion
                  FROM public.pedido_adquisicion
                 WHERE pedido_id=%s
            """, (pedido_id,))
            head = cur.fetchone()
            if head:
                cur.execute("""
                    SELECT descripcion, cantidad, unidad, precio_unitario, total
                      FROM public.pedido_adquisicion_item
                     WHERE pedido_id=%s
                     ORDER BY id
                """, (pedido_id,))
                items = cur.fetchall()
                out["modulo"] = {"tipo": "adquisicion", **head, "items": items}
                return out

            # Reparación
            cur.execute("""
                SELECT tipo_reparacion, unidad_reparar, que_reparar, detalle_reparacion
                  FROM public.pedido_reparacion
                 WHERE pedido_id=%s
            """, (pedido_id,))
            r = cur.fetchone()
            if r:
                out["modulo"] = {"tipo": "reparacion", **r}
                return out

            out["modulo"] = None
            return out

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ui_pedido_detalle_error: {e}")

# =========================
# Cambio de estado
# =========================

EstadoNuevo = Literal["aprobado", "en_revision"]

class EstadoIn(BaseModel):
    estado: EstadoNuevo
    motivo: Optional[str] = None  # auditoría

UMBRAL = Decimal(10_000_000)  # $10M

def _infer_role(nombre_secretaria: Optional[str]) -> str:
    s = (nombre_secretaria or "").upper()
    if "ECONOM" in s:  # Secretaría de Economía
        return "economia_admin"
    if "ÁREA DE COMPRAS" in s or "AREA DE COMPRAS" in s:
        return "area_compras"
    if "SECRETARÍA DE COMPRAS" in s or "SECRETARIA DE COMPRAS" in s:
        return "secretaria_compras"
    return "secretaria"

@router.post("/pedidos/{pedido_id}/estado")
def ui_pedidos_set_estado(
    pedido_id: int,
    body: EstadoIn,
    x_user: Optional[str] = Header(default=None),
    x_secretaria: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            # Fila con lock
            cur.execute(
                """
                SELECT p.id, p.numero, p.estado, p.presupuesto_estimado,
                       p.secretaria_id, s.nombre AS secretaria_nombre
                  FROM public.pedido p
                  JOIN public.secretaria s ON s.id = p.secretaria_id
                 WHERE p.id = %s
                 FOR UPDATE
                """,
                (pedido_id,)
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Pedido no encontrado")

            estado_anterior = row["estado"]
            estado_nuevo = body.estado
            monto: Decimal = row["presupuesto_estimado"] or Decimal(0)
            sec_nombre: str = row["secretaria_nombre"]

            # Permisos
            rol = _infer_role(x_secretaria)
            allowed = False
            if rol == "economia_admin":
                allowed = True
            elif rol == "area_compras":
                allowed = (monto > UMBRAL)
            elif rol == "secretaria_compras":
                allowed = (monto <= UMBRAL)
            else:
                if not x_secretaria:
                    raise HTTPException(status_code=403, detail="Falta X-Secretaria para validar permisos")
                allowed = (x_secretaria.strip().upper() == (sec_nombre or "").strip().upper())

            if not allowed:
                raise HTTPException(status_code=403, detail="No tenés permisos para cambiar el estado de este pedido")

            # Idempotencia
            if estado_anterior == estado_nuevo:
                return {
                    "ok": True,
                    "id": pedido_id,
                    "numero": row["numero"],
                    "estado": estado_nuevo,
                    "previous": estado_anterior,
                    "updated": False,
                    "message": "Sin cambios",
                }

            # Update
            cur.execute(
                """
                UPDATE public.pedido
                   SET estado = %s,
                       updated_at = NOW()
                 WHERE id = %s
             RETURNING updated_at
                """,
                (estado_nuevo, pedido_id)
            )
            upd = cur.fetchone()
            if not upd:
                raise HTTPException(status_code=500, detail="No se pudo actualizar el pedido")

            # Historial
            cur.execute(
                """
                INSERT INTO public.pedido_historial
                    (pedido_id, estado_anterior, estado_nuevo, motivo, changed_by)
                VALUES
                    (%s,        %s,              %s,           %s,     %s)
                """,
                (
                    pedido_id,
                    estado_anterior,
                    estado_nuevo,
                    body.motivo,
                    x_user or "ui",
                )
            )

            return {
                "ok": True,
                "id": pedido_id,
                "numero": row["numero"],
                "estado": estado_nuevo,
                "previous": estado_anterior,
                "updated": True,
                "updated_at": upd["updated_at"],
            }

    except (OperationalError, DatabaseError) as e:
        raise HTTPException(status_code=500, detail=f"Error actualizando estado: {e}")

# =========================
# Archivos
# =========================

def _pedido_estado(conn, pedido_id: int) -> str:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT estado FROM public.pedido WHERE id = %s", (pedido_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Pedido no encontrado")
        return row["estado"]

@router.get("/pedidos/{pedido_id}/archivos")
def ui_list_archivos(pedido_id: int) -> Dict[str, Any]:
    """
    Lista adjuntos desde public.pedido_archivo.
    Respuesta: { items: [{ id, kind, filename, size_bytes, url, uploaded_at, download }] }
    """
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT
                  id,
                  tipo_doc      AS kind,
                  file_name     AS filename,
                  content_type,
                  bytes         AS size_bytes,
                  storage_path  AS url,
                  created_at    AS uploaded_at
                FROM public.pedido_archivo
               WHERE pedido_id = %s
               ORDER BY created_at DESC, id DESC
            """, (pedido_id,))
            rows = cur.fetchall() or []

        items = [{
            "id": r["id"],
            "kind": r["kind"],
            "filename": r["filename"],
            "content_type": r["content_type"],
            "size_bytes": r["size_bytes"],
            "url": r["url"],
            "uploaded_at": r["uploaded_at"].isoformat() if r["uploaded_at"] else None,
            "download": f"/pedidos/archivos/{r['id']}/download",
        } for r in rows]

        return {"items": items}
    except (OperationalError, DatabaseError) as e:
        raise HTTPException(status_code=500, detail=f"Error listando archivos: {e}")

@router.post("/pedidos/{pedido_id}/archivo/formal")
async def ui_upload_formal(pedido_id: int, pdf: UploadFile = File(...)) -> Dict[str, Any]:
    """
    Compatibilidad con la UI antigua:
    sube el 'PDF firmado' como tipo_doc=formal_pdf usando el endpoint nuevo.
    Requiere estado 'aprobado' (se valida en upload_archivo → luego pasa a 'en_proceso').
    """
    if not pdf or not pdf.filename:
        raise HTTPException(status_code=400, detail="Falta PDF")
    if pdf.content_type not in ("application/pdf",):
        raise HTTPException(status_code=415, detail="Sólo se acepta PDF")

    # delega en /pedidos/{pedido_id}/archivos con tipo_doc=formal_pdf
    return await upload_archivo(
        pedido_id=pedido_id,
        tipo_doc="formal_pdf",
        archivo=pdf,
    )
