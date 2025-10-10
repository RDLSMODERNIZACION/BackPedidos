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

router = APIRouter(prefix="/ui", tags=["ui"])

FILES_DIR = os.getenv("FILES_DIR", "files")

# =========================
# Listado (ya existente)
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

# =========================
# Cambio de estado (NUEVO)
# =========================

# Sólo estas dos acciones, según tu pedido:
EstadoNuevo = Literal["aprobado", "en_revision"]

class EstadoIn(BaseModel):
    estado: EstadoNuevo
    motivo: Optional[str] = None  # opcional, para auditoría

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
    # 👇 IMPORTANTE: sin convert_underscores=False para aceptar "X-User" y "X-Secretaria"
    x_user: Optional[str] = Header(default=None),
    x_secretaria: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """
    Cambia el estado del pedido en public.pedido y registra auditoría en public.pedido_historial.
    Reglas de permisos:
      - Economía (admin): puede todo.
      - Área de Compras: sólo si presupuesto_estimado > $10M.
      - Secretaría de Compras: sólo si presupuesto_estimado ≤ $10M.
      - Resto de secretarías: sólo los propios (mismo nombre de secretaría).
    """
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            # 1) Tomar la fila con lock y traer info necesaria
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

            # 2) Permisos (lado servidor)
            rol = _infer_role(x_secretaria)
            allowed = False
            if rol == "economia_admin":
                allowed = True
            elif rol == "area_compras":
                allowed = (monto > UMBRAL)
            elif rol == "secretaria_compras":
                allowed = (monto <= UMBRAL)
            else:
                # resto de secretarías: sólo los propios
                if not x_secretaria:
                    raise HTTPException(status_code=403, detail="Falta X-Secretaria para validar permisos")
                allowed = (x_secretaria.strip().upper() == (sec_nombre or "").strip().upper())

            if not allowed:
                raise HTTPException(status_code=403, detail="No tenés permisos para cambiar el estado de este pedido")

            # 3) Idempotencia
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

            # 4) Actualizar el pedido
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

            # 5) Auditoría en historial
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

            # commit implícito al salir del with
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
# Archivos (NUEVO)
# =========================

def _pedido_estado(conn, pedido_id: int) -> str:
    with conn.cursor(row_factory=dict_row) as cur:
        # Tu FK referencia public.pedido (singular)
        cur.execute("SELECT estado FROM public.pedido WHERE id = %s", (pedido_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Pedido no encontrado")
        return row["estado"]

@router.get("/pedidos/{pedido_id}/archivos")
def ui_pedido_list_archivos(pedido_id: int) -> List[Dict[str, Any]]:
    """
    Lista archivos del pedido desde public.pedido_archivo, con alias para el front.
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
               ORDER BY created_at DESC
            """, (pedido_id,))
            return cur.fetchall()
    except (OperationalError, DatabaseError) as e:
        raise HTTPException(status_code=500, detail=f"Error listando archivos: {e}")

@router.post("/pedidos/{pedido_id}/archivo/formal")
def ui_pedido_upload_formal(pedido_id: int, pdf: UploadFile = File(...)) -> Dict[str, Any]:
    """
    Sube/reescribe el PDF formal firmado (tipo_doc='formal_pdf') usando public.pedido_archivo.
    Requiere que el pedido esté EN estado 'aprobado'.
    Guarda el archivo en {FILES_DIR}/pedidos/<id>/formal.pdf y hace UPSERT contra (pedido_id, tipo_doc).
    """
    if pdf.content_type not in ("application/pdf",):
        raise HTTPException(status_code=415, detail="Sólo se acepta PDF")

    try:
        with get_conn() as conn:
            estado = _pedido_estado(conn, pedido_id)
            if estado != "aprobado":
                raise HTTPException(status_code=409, detail=f"El pedido no está aprobado (estado actual: {estado})")

            # Guardar en disco
            pedido_dir = os.path.join(FILES_DIR, "pedidos", str(pedido_id))
            os.makedirs(pedido_dir, exist_ok=True)
            dest_path = os.path.join(pedido_dir, "formal.pdf")
            with open(dest_path, "wb") as out:
                shutil.copyfileobj(pdf.file, out)

            url = f"/files/pedidos/{pedido_id}/formal.pdf"
            size = os.path.getsize(dest_path)

            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""
                    INSERT INTO public.pedido_archivo
                        (pedido_id, storage_path, file_name, content_type, bytes, tipo_doc)
                    VALUES (%s, %s, %s, %s, %s, 'formal_pdf')
                    ON CONFLICT (pedido_id, tipo_doc)
                    DO UPDATE SET
                        storage_path = EXCLUDED.storage_path,
                        file_name    = EXCLUDED.file_name,
                        content_type = EXCLUDED.content_type,
                        bytes        = EXCLUDED.bytes,
                        created_at   = NOW()
                    RETURNING
                      id,
                      tipo_doc      AS kind,
                      file_name     AS filename,
                      content_type,
                      bytes         AS size_bytes,
                      storage_path  AS url,
                      created_at    AS uploaded_at
                """, (pedido_id, url, pdf.filename, pdf.content_type, size))
                row = cur.fetchone()
                conn.commit()
                return row
    except (OperationalError, DatabaseError) as e:
        raise HTTPException(status_code=500, detail=f"Error subiendo formal.pdf: {e}")
