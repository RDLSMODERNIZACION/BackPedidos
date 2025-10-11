# pp/routes/pedidos.py
# Nota: si tu proyecto usa el paquete "app", este archivo puede ir como app/routes/pedidos.py
# y el import de get_conn (abajo) ya queda bien. Si usás "pp", cambiá a: from pp.db import get_conn

from fastapi import APIRouter, HTTPException, UploadFile, File, Query, Form
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from typing import Optional, Literal, List, Union, Dict, Any
from psycopg.rows import dict_row
from datetime import date
import os
from uuid import uuid4
import httpx

from app.db import get_conn  # ⇐ ajustá a tu paquete si es distinto

router = APIRouter(prefix="/pedidos", tags=["pedidos"])

# =========================
# Config Supabase Storage
# =========================
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "pedidos-prod")

# Permitidos (incluye detonadores de estado)
ALLOWED_TIPO_DOC = {
    "presupuesto_1",
    "presupuesto_2",
    "anexo1_obra",
    "formal_pdf",   # → en_proceso
    "expediente_1", # → area_pago
    "expediente_2", # → cerrado
}

# =========================
# Pydantic: Entrada
# =========================
class GeneralIn(BaseModel):
    secretaria: str
    estado: Literal[
        "borrador",
        "enviado",
        "en_revision",
        "aprobado",
        "rechazado",
        "en_proceso",
        "area_pago",
        "cerrado",
    ] = "enviado"
    fecha_pedido: Optional[date] = None
    fecha_desde: Optional[date] = None
    fecha_hasta: Optional[date] = None
    presupuesto_estimado: Optional[float] = None
    observaciones: Optional[str] = None
    created_by_username: Optional[str] = None  # opcional

AmbitoTipo = Literal["ninguno", "obra", "mantenimientodeescuelas"]

class AmbitoObraIn(BaseModel):
    obra_nombre: str = Field(..., description="Nombre de la obra (Obra)")

class AmbitoEscuelaIn(BaseModel):
    escuela: str = Field(..., description="Nombre de la escuela (Mantenimiento de Escuelas)")

class AmbitoIn(BaseModel):
    tipo: AmbitoTipo
    obra: Optional[AmbitoObraIn] = None
    escuelas: Optional[AmbitoEscuelaIn] = None  # 'escuelas' para no chocar con palabra reservada

# ======== SERVICIOS (actualizado: otros / profesionales) ========
class ServiciosIn(BaseModel):
    tipo: Literal["servicios"]
    tipo_servicio: Literal["otros", "profesionales"]
    # “otros”
    servicio_requerido: Optional[str] = None
    destino_servicio: Optional[str] = None
    # “profesionales”
    tipo_profesional: Optional[str] = None
    dia_desde: Optional[date] = None
    dia_hasta: Optional[date] = None

class AlquilerIn(BaseModel):
    tipo: Literal["alquiler"]
    categoria: Literal["edificio", "maquinaria", "otros"]
    uso_edificio: Optional[str] = None
    ubicacion_edificio: Optional[str] = None
    uso_maquinaria: Optional[str] = None
    tipo_maquinaria: Optional[str] = None
    requiere_combustible: Optional[bool] = None
    requiere_chofer: Optional[bool] = None
    cronograma_desde: Optional[date] = None
    cronograma_hasta: Optional[date] = None
    horas_por_dia: Optional[float] = None
    que_alquilar: Optional[str] = None
    detalle_uso: Optional[str] = None

class AdqItemIn(BaseModel):
    descripcion: str
    cantidad: float = 1
    unidad: Optional[str] = None
    precio_unitario: Optional[float] = None

class AdquisicionIn(BaseModel):
    tipo: Literal["adquisicion"]
    proposito: Optional[str] = None
    modo_adquisicion: Literal["uno", "muchos"] = "uno"
    items: List[AdqItemIn] = []

class ReparacionIn(BaseModel):
    tipo: Literal["reparacion"]
    tipo_reparacion: Literal["maquinaria", "otros"]
    unidad_reparar: Optional[str] = None
    que_reparar: Optional[str] = None
    detalle_reparacion: Optional[str] = None

ModuloIn = Union[ServiciosIn, AlquilerIn, AdquisicionIn, ReparacionIn]

class PedidoCreate(BaseModel):
    generales: GeneralIn
    ambito: AmbitoIn
    modulo: ModuloIn

# =========================
# Helpers DB
# =========================
def _one_or_none(rows: list[dict]) -> Optional[dict]:
    return rows[0] if rows else None

def _lookup_secretaria_id(cur, nombre: str) -> int:
    cur.execute("SELECT id FROM public.secretaria WHERE nombre=%s", (nombre,))
    row = _one_or_none(cur.fetchall())
    if not row:
        raise HTTPException(status_code=400, detail=f"Secretaría no encontrada: {nombre}")
    return row["id"]

def _set_estado(cur, pedido_id: int, nuevo: str, changed_by: Optional[str] = None):
    """Cambia estado en public.pedido y registra en pedido_historial (si cambió)."""
    cur.execute("SELECT estado FROM public.pedido WHERE id=%s", (pedido_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Pedido no encontrado")
    anterior = row["estado"]
    if anterior == nuevo:
        return
    cur.execute(
        "UPDATE public.pedido SET estado=%s, updated_at=now() WHERE id=%s",
        (nuevo, pedido_id),
    )
    # pedido_historial: id, pedido_id, estado_anterior, estado_nuevo, motivo, changed_by, created_at
    cur.execute(
        """
        INSERT INTO public.pedido_historial
          (pedido_id, estado_anterior, estado_nuevo, motivo, changed_by)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (pedido_id, anterior, nuevo, None, changed_by or "system"),
    )

# =========================
# Catálogo de Escuelas
# =========================
class EscuelaIn(BaseModel):
    nombre: str = Field(..., min_length=2, description="Nombre visible de la escuela")
    ubicacion: Optional[str] = None
    activa: Optional[bool] = True

def _ensure_catalog_escuela(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS public.catalog_escuela (
          id         BIGSERIAL PRIMARY KEY,
          nombre     TEXT NOT NULL UNIQUE,
          ubicacion  TEXT,
          activa     BOOLEAN NOT NULL DEFAULT TRUE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_catalog_escuela_nombre
            ON public.catalog_escuela (nombre);
    """)

@router.get("/catalogo/escuelas")
def catalogo_escuelas(
    q: Optional[str] = Query(None, description="Filtro por nombre"),
    activa: Optional[bool] = Query(True, description="Sólo activas por defecto"),
    limit: int = Query(500, ge=1, le=5000),
) -> List[Dict[str, Any]]:
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            _ensure_catalog_escuela(cur)
            where, params = [], {}
            if q:
                where.append("nombre ILIKE %(q)s"); params["q"] = f"%{q}%"
            if activa is not None:
                where.append("activa = %(activa)s"); params["activa"] = activa
            where_sql = "WHERE " + " AND ".join(where) if where else ""
            cur.execute(f"""
                SELECT id, nombre, ubicacion
                  FROM public.catalog_escuela
                  {where_sql}
                 ORDER BY nombre ASC
                 LIMIT {limit}
            """, params)
            return cur.fetchall()
    except Exception:
        try:
            with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur2:
                if q:
                    cur2.execute("""
                        SELECT DISTINCT ON (TRIM(escuela)) NULL::BIGINT AS id,
                               TRIM(escuela)                AS nombre,
                               NULL::TEXT                   AS ubicacion
                          FROM public.ambito_mant_escuela
                         WHERE escuela ILIKE %s
                         ORDER BY TRIM(escuela)
                         LIMIT %s
                    """, (f"%{q}%", limit))
                else:
                    cur2.execute("""
                        SELECT DISTINCT ON (TRIM(escuela)) NULL::BIGINT AS id,
                               TRIM(escuela)                AS nombre,
                               NULL::TEXT                   AS ubicacion
                          FROM public.ambito_mant_escuela
                         ORDER BY TRIM(escuela)
                         LIMIT %s
                    """, (limit,))
                return cur2.fetchall()
        except Exception as e2:
            raise HTTPException(status_code=500, detail=f"Error listando escuelas: {e2}")

@router.post("/catalogo/escuelas", status_code=201)
def catalogo_escuelas_create(body: EscuelaIn) -> Dict[str, Any]:
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            _ensure_catalog_escuela(cur)
            cur.execute("""
                INSERT INTO public.catalog_escuela (nombre, ubicacion, activa)
                VALUES (%s, %s, COALESCE(%s, TRUE))
                ON CONFLICT (nombre) DO UPDATE
                   SET ubicacion = EXCLUDED.ubicacion,
                       activa    = COALESCE(EXCLUDED.activa, public.catalog_escuela.activa),
                       updated_at= now()
                RETURNING id, nombre, ubicacion, activa
            """, (body.nombre.strip(), (body.ubicacion or None), body.activa))
            return cur.fetchone()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creando escuela: {e}")

# =========================
# Catálogo de Obras
# =========================
class ObraCatIn(BaseModel):
    nombre: str = Field(..., min_length=2, description="Nombre visible de la obra/lugar")
    ubicacion: Optional[str] = None
    activa: Optional[bool] = True

def _ensure_catalog_obra(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS public.catalog_obra (
          id         BIGSERIAL PRIMARY KEY,
          nombre     TEXT NOT NULL UNIQUE,
          ubicacion  TEXT,
          activa     BOOLEAN NOT NULL DEFAULT TRUE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_catalog_obra_nombre ON public.catalog_obra (nombre);")

@router.get("/catalogo/obras")
def catalogo_obras(
    q: Optional[str] = Query(None),
    activa: Optional[bool] = Query(True),
    limit: int = Query(500, ge=1, le=5000),
) -> List[Dict[str, Any]]:
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            _ensure_catalog_obra(cur)
            where, params = [], {}
            if q:
                where.append("nombre ILIKE %(q)s"); params["q"] = f"%{q}%"
            if activa is not None:
                where.append("activa = %(activa)s"); params["activa"] = activa
            where_sql = "WHERE " + " AND ".join(where) if where else ""
            cur.execute(f"""
                SELECT id, nombre, ubicacion
                  FROM public.catalog_obra
                  {where_sql}
                 ORDER BY nombre ASC
                 LIMIT {limit}
            """, params)
            return cur.fetchall()
    except Exception:
        try:
            with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur2:
                if q:
                    cur2.execute("""
                        SELECT DISTINCT ON (TRIM(nombre_obra)) NULL::BIGINT AS id,
                               TRIM(nombre_obra) AS nombre,
                               NULL::TEXT        AS ubicacion
                        FROM public.ambito_obra
                        WHERE nombre_obra ILIKE %s
                        ORDER BY TRIM(nombre_obra)
                        LIMIT %s
                    """, (f"%{q}%", limit))
                else:
                    cur2.execute("""
                        SELECT DISTINCT ON (TRIM(nombre_obra)) NULL::BIGINT AS id,
                               TRIM(nombre_obra) AS nombre,
                               NULL::TEXT        AS ubicacion
                        FROM public.ambito_obra
                        ORDER BY TRIM(nombre_obra)
                        LIMIT %s
                    """, (limit,))
                return cur2.fetchall()
        except Exception as e2:
            raise HTTPException(status_code=500, detail=f"Error listando obras: {e2}")

@router.post("/catalogo/obras", status_code=201)
def catalogo_obras_create(body: ObraCatIn) -> Dict[str, Any]:
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            _ensure_catalog_obra(cur)
            cur.execute("""
                INSERT INTO public.catalog_obra (nombre, ubicacion, activa)
                VALUES (%s, %s, COALESCE(%s, TRUE))
                ON CONFLICT (nombre) DO UPDATE
                   SET ubicacion = EXCLUDED.ubicacion,
                       activa    = COALESCE(EXCLUDED.activa, public.catalog_obra.activa),
                       updated_at= now()
                RETURNING id, nombre, ubicacion, activa
            """, (body.nombre.strip(), (body.ubicacion or None), body.activa))
            return cur.fetchone()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creando obra: {e}")

# =========================
# Catálogo de Unidades
# =========================
class UnidadIn(BaseModel):
    dominio: Optional[str] = None
    unidad_nro: Optional[int] = None
    marca: Optional[str] = None
    modelo: Optional[str] = None
    activa: Optional[bool] = True

def _ensure_catalog_unidad(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS public.catalog_unidad (
          id           BIGSERIAL PRIMARY KEY,
          dominio      TEXT,
          unidad_nro   INTEGER UNIQUE,
          marca        TEXT,
          modelo       TEXT,
          activa       BOOLEAN NOT NULL DEFAULT TRUE,
          created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
          CHECK (dominio IS NOT NULL OR unidad_nro IS NOT NULL)
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_catalog_unidad_marca ON public.catalog_unidad (marca);")

@router.get("/catalogo/unidades")
def catalogo_unidades(
    q: Optional[str] = Query(None, description="Busca en dominio, marca o modelo"),
    marca: Optional[str] = Query(None),
    activa: Optional[bool] = Query(True),
    limit: int = Query(1000, ge=1, le=10000),
) -> List[Dict[str, Any]]:
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            _ensure_catalog_unidad(cur)
            where, params = [], {}
            if q:
                where.append("(COALESCE(dominio,'') ILIKE %(q)s OR COALESCE(marca,'') ILIKE %(q)s OR COALESCE(modelo,'') ILIKE %(q)s)")
                params["q"] = f"%{q}%"
            if marca:
                where.append("marca ILIKE %(marca)s"); params["marca"] = f"%{marca}%"
            if activa is not None:
                where.append("activa = %(activa)s"); params["activa"] = activa
            where_sql = "WHERE " + " AND ".join(where) if where else ""
            cur.execute(f"""
                SELECT id, dominio, unidad_nro, marca, modelo, activa
                  FROM public.catalog_unidad
                  {where_sql}
                 ORDER BY COALESCE(unidad_nro, 0) ASC, dominio NULLS LAST
                 LIMIT {limit}
            """, params)
            return cur.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error listando unidades: {e}")

@router.get("/catalogo/unidades/{unidad_nro}")
def catalogo_unidad_por_nro(unidad_nro: int) -> Dict[str, Any]:
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            _ensure_catalog_unidad(cur)
            cur.execute("""
                SELECT id, dominio, unidad_nro, marca, modelo, activa
                  FROM public.catalog_unidad
                 WHERE unidad_nro = %s
            """, (unidad_nro,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Unidad no encontrada")
            return row
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error buscando unidad: {e}")

@router.post("/catalogo/unidades", status_code=201)
def catalogo_unidades_create(body: UnidadIn) -> Dict[str, Any]:
    if not body.unidad_nro and not body.dominio:
        raise HTTPException(status_code=422, detail="Debe informar al menos unidad_nro o dominio")
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            _ensure_catalog_unidad(cur)

            if body.unidad_nro is not None:
                cur.execute("""
                    INSERT INTO public.catalog_unidad (dominio, unidad_nro, marca, modelo, activa)
                    VALUES (%s, %s, %s, %s, COALESCE(%s, TRUE))
                    ON CONFLICT (unidad_nro) DO UPDATE
                      SET dominio   = EXCLUDED.dominio,
                          marca     = EXCLUDED.marca,
                          modelo    = EXCLUDED.modelo,
                          activa    = EXCLUDED.activa,
                          updated_at= now()
                    RETURNING id, dominio, unidad_nro, marca, modelo, activa
                """, (body.dominio, body.unidad_nro, body.marca, body.modelo, body.activa))
                return cur.fetchone()

            if body.dominio:
                cur.execute("SELECT id FROM public.catalog_unidad WHERE dominio = %s", (body.dominio,))
                rows = cur.fetchall()
                if len(rows) == 1:
                    uid = rows[0]["id"]
                    cur.execute("""
                        UPDATE public.catalog_unidad
                           SET marca = COALESCE(%s, marca),
                               modelo = COALESCE(%s, modelo),
                               activa = COALESCE(%s, activa),
                               updated_at = now()
                         WHERE id = %s
                     RETURNING id, dominio, unidad_nro, marca, modelo, activa
                    """, (body.marca, body.modelo, body.activa, uid))
                    return cur.fetchone()

                cur.execute("""
                    INSERT INTO public.catalog_unidad (dominio, marca, modelo, activa)
                    VALUES (%s, %s, %s, COALESCE(%s, TRUE))
                 RETURNING id, dominio, unidad_nro, marca, modelo, activa
                """, (body.dominio, body.marca, body.modelo, body.activa))
                return cur.fetchone()

            raise HTTPException(status_code=422, detail="Datos insuficientes para crear unidad")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creando unidad: {e}")

# =========================
# POST /pedidos (creación)
# =========================
@router.post("", status_code=201)
def create_pedido_full(payload: PedidoCreate) -> Dict[str, Any]:
    g = payload.generales
    a = payload.ambito
    m = payload.modulo

    MAP_AMBITO = {
        "ninguno": "general",
        "obra": "obra",
        "mantenimientodeescuelas": "mant_escuela",
    }
    tipo_ui = a.tipo
    tipo_db = MAP_AMBITO.get(tipo_ui)
    if tipo_db is None:
        raise HTTPException(status_code=422, detail=f"Ambito inválido: {tipo_ui}")

    if tipo_ui == "obra" and not a.obra:
        raise HTTPException(status_code=400, detail="Falta 'ambito.obra' para tipo=obra.")
    if tipo_ui == "mantenimientodeescuelas" and not a.escuelas:
        raise HTTPException(status_code=400, detail="Falta 'ambito.escuelas' para tipo=mantenimientodeescuelas.")

    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            # 1) Secretaría
            sec_id = _lookup_secretaria_id(cur, g.secretaria)

            # 2) Pedido
            cur.execute("""
                INSERT INTO public.pedido (
                  secretaria_id, estado, fecha_pedido, fecha_desde, fecha_hasta,
                  presupuesto_estimado, observaciones, created_by
                )
                VALUES (
                  %s, %s,
                  COALESCE(%s, CURRENT_DATE),
                  %s, %s,
                  %s, %s,
                  (SELECT user_id FROM public.perfil WHERE login_username=%s LIMIT 1)
                )
                RETURNING id, numero, created_at, updated_at
            """, (
                sec_id, g.estado, g.fecha_pedido, g.fecha_desde, g.fecha_hasta,
                g.presupuesto_estimado, g.observaciones, g.created_by_username
            ))
            ped = cur.fetchone()
            pedido_id = ped["id"]

            # 3) Ámbito
            cur.execute("""
                INSERT INTO public.pedido_ambito (pedido_id, tipo)
                VALUES (%s, %s)
            """, (pedido_id, tipo_db))

            if tipo_ui == "obra":
                cur.execute("""
                  INSERT INTO public.ambito_obra (pedido_id, nombre_obra)
                  VALUES (%s, %s)
                """, (pedido_id, a.obra.obra_nombre))
            elif tipo_ui == "mantenimientodeescuelas":
                cur.execute("""
                  INSERT INTO public.ambito_mant_escuela (pedido_id, escuela)
                  VALUES (%s, %s)
                """, (pedido_id, a.escuelas.escuela))

            # 4) MÓDULO: insertar detalle según m.tipo (SERVICIOS actualizado)
            if m.tipo == "servicios":
                if m.tipo_servicio == "otros":
                    cur.execute("""
                        INSERT INTO public.pedido_servicios
                          (pedido_id, tipo_servicio, servicio_requerido, destino_servicio)
                        VALUES (%s, %s, %s, %s)
                    """, (pedido_id, "otros", m.servicio_requerido, m.destino_servicio))
                else:  # profesionales
                    cur.execute("""
                        INSERT INTO public.pedido_servicios
                          (pedido_id, tipo_servicio, tipo_profesional, dia_desde, dia_hasta)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (pedido_id, "profesionales", m.tipo_profesional, m.dia_desde, m.dia_hasta))

            # (a futuro podés agregar bloques para alquiler/adquisicion/reparacion)

            return {
                "ok": True,
                "pedido_id": pedido_id,
                "numero": ped["numero"],
                "created_at": ped["created_at"],
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"create_error: {e}")

# =========================
# Anexos (Supabase Storage) + transiciones automáticas de estado
# =========================
def _sb_object_path(pedido_id: int, tipo_doc: str, filename: str) -> str:
    safe = (filename or "archivo.pdf").replace("/", "_").replace("\\", "_").strip()
    return f"pedido_{pedido_id}/{tipo_doc}/{uuid4()}_{safe}"

@router.post("/{pedido_id}/archivos")
async def upload_archivo(
    pedido_id: int,
    tipo_doc: str = Form(...),           # 'anexo1_obra' | 'formal_pdf' | 'presupuesto_1' | 'presupuesto_2' | 'expediente_1' | 'expediente_2'
    archivo: UploadFile = File(...),     # clave "archivo" en multipart/form-data
):
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=500, detail="Faltan SUPABASE_URL o SUPABASE_SERVICE_ROLE_KEY en el backend")
    if tipo_doc not in ALLOWED_TIPO_DOC:
        raise HTTPException(status_code=400, detail=f"tipo_doc inválido: {tipo_doc}")
    if not archivo.filename:
        raise HTTPException(status_code=400, detail="Falta nombre de archivo")
    mime = archivo.content_type or "application/pdf"
    if mime != "application/pdf":
        raise HTTPException(status_code=415, detail=f"Solo se aceptan PDF. Recibido: {mime}")

    data: bytes = await archivo.read()
    if not data:
        raise HTTPException(status_code=400, detail="El archivo llegó vacío (0 bytes)")

    # Verificar pedido existe
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT 1 FROM public.pedido WHERE id = %s;", (pedido_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Pedido no encontrado")

    # Subir a storage
    object_key = _sb_object_path(pedido_id, tipo_doc, archivo.filename)
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{object_key}"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            upload_url,
            headers={
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": mime,
                "x-upsert": "true",
            },
            content=data,
        )
        if r.status_code not in (200, 201):
            raise HTTPException(status_code=r.status_code, detail=f"Error subiendo a Storage: {r.text}")

    storage_path = f"supabase://{SUPABASE_BUCKET}/{object_key}"
    size = len(data)

    # Guardar metadatos en DB
    sql = """
    INSERT INTO public.pedido_archivo
      (pedido_id, storage_path, file_name, content_type, bytes, tipo_doc)
    VALUES
      (%s,        %s,           %s,        %s,           %s,    %s)
    ON CONFLICT (pedido_id, tipo_doc)
    DO UPDATE SET
      storage_path = EXCLUDED.storage_path,
      file_name    = EXCLUDED.file_name,
      content_type = EXCLUDED.content_type,
      bytes        = EXCLUDED.bytes,
      created_at   = now()
    RETURNING id;
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (pedido_id, storage_path, archivo.filename, mime, size, tipo_doc))
        row = cur.fetchone()
        conn.commit()

    # Transiciones automáticas según tipo_doc
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT estado FROM public.pedido WHERE id=%s", (pedido_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Pedido no encontrado")
        estado = r["estado"]

        if tipo_doc == "formal_pdf":
            if estado in ("borrador", "enviado", "en_revision", "aprobado"):
                _set_estado(cur, pedido_id, "en_proceso", changed_by="upload:formal_pdf")

        elif tipo_doc == "expediente_1":
            if estado != "cerrado":
                _set_estado(cur, pedido_id, "area_pago", changed_by="upload:expediente_1")

        elif tipo_doc == "expediente_2":
            if estado != "cerrado":
                _set_estado(cur, pedido_id, "cerrado", changed_by="upload:expediente_2")

        conn.commit()

    return {"ok": True, "archivo_id": row["id"], "bytes": size, "path": storage_path}

@router.get("/archivos/{archivo_id}/signed")
async def get_signed_download(archivo_id: int, expires_sec: int = 600):
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=500, detail="Faltan SUPABASE_URL o SUPABASE_SERVICE_ROLE_KEY en el backend")

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT storage_path, file_name, content_type
            FROM public.pedido_archivo
            WHERE id = %s
        """, (archivo_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Archivo no encontrado")

    storage_path: str = row["storage_path"] or ""
    if not storage_path.startswith("supabase://"):
        raise HTTPException(status_code=400, detail="Este archivo no está en Supabase Storage")

    _, bucket_and_key = storage_path.split("://", 1)
    bucket, key = bucket_and_key.split("/", 1)

    sign_url = f"{SUPABASE_URL}/storage/v1/object/sign/{bucket}/{key}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            sign_url,
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"},
            json={"expiresIn": expires_sec},
        )
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=f"Error firmando URL: {r.text}")
        payload = r.json()
        signed_url = f"{SUPABASE_URL}{payload['signedURL']}"

    return {
        "url": signed_url,
        "file_name": row["file_name"],
        "content_type": row["content_type"],
        "expires_in": expires_sec,
    }

@router.get("/archivos/{archivo_id}/download")
async def download_redirect(archivo_id: int, expires_sec: int = 600):
    """
    Redirige (307) a la Signed URL de Supabase. Ideal para <a href="...">Descargar</a>.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=500, detail="Faltan SUPABASE_URL o SUPABASE_SERVICE_ROLE_KEY")

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT storage_path FROM public.pedido_archivo WHERE id=%s", (archivo_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Archivo no encontrado")

    storage_path: str = row["storage_path"] or ""
    if not storage_path.startswith("supabase://"):
        raise HTTPException(status_code=400, detail="Este archivo no está en Supabase Storage")

    _, bucket_and_key = storage_path.split("://", 1)
    bucket, key = bucket_and_key.split("/", 1)

    sign_url = f"{SUPABASE_URL}/storage/v1/object/sign/{bucket}/{key}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            sign_url,
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"},
            json={"expiresIn": expires_sec},
        )
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=f"Error firmando URL: {r.text}")
        payload = r.json()
        signed_url = f"{SUPABASE_URL}{payload['signedURL']}"

    return RedirectResponse(url=signed_url, status_code=307)

# Compat: endpoint anterior (clave "file"), delega al nuevo
@router.post("/{pedido_id}/archivos/anexo1_obra")
async def upload_anexo1_obra(pedido_id: int, file: UploadFile = File(...)):
    return await upload_archivo(pedido_id=pedido_id, tipo_doc="anexo1_obra", archivo=file)
