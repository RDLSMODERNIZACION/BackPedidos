# pp/routes/pedidos.py
# Si tu proyecto usa el paquete "app", este archivo puede ir como app/routes/pedidos.py
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

# Permitidos (solo presupuestos y anexo de obra)
ALLOWED_TIPO_DOC = {
    "presupuesto_1",
    "presupuesto_2",
    "anexo1_obra",
}

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
# POST /pedidos (creación) — formato v2 del front
# =========================
class V2Generales(BaseModel):
    secretaria: str
    fecha_pedido: Optional[date] = None
    fecha_desde: Optional[date] = None
    fecha_hasta: Optional[date] = None
    presupuesto_estimado: Optional[str | float] = None
    observaciones: Optional[str] = None
    estado: Optional[Literal[
        "borrador","enviado","en_revision","aprobado","rechazado","en_proceso","area_pago","cerrado"
    ]] = "enviado"
    created_by_username: Optional[str] = None

class V2ModuloDraft(BaseModel):
    modulo: Literal["servicios","alquiler","adquisicion","reparacion"]
    payload: Dict[str, Any]

class PedidoV2(BaseModel):
    generales: V2Generales
    modulo_seleccionado: Literal["servicios","alquiler","adquisicion","reparacion"]
    modulo_draft: V2ModuloDraft
    ambitoIncluido: Literal["ninguno","obra","mantenimientodeescuelas"]
    especiales: Optional[Dict[str, Any]] = None

@router.post("", status_code=201)
def create_pedido_simple(body: PedidoV2) -> Dict[str, Any]:
    g = body.generales
    md = body.modulo_draft
    ambitoIncluido = body.ambitoIncluido

    AMBITO_MAP = {
        "ninguno": "general",
        "obra": "obra",
        "mantenimientodeescuelas": "mant_escuela",
    }
    tipo_ambito_db = AMBITO_MAP[ambitoIncluido]

    # normalizar presupuesto
    presu: Optional[float] = None
    if isinstance(g.presupuesto_estimado, (int, float)):
        presu = float(g.presupuesto_estimado)
    elif isinstance(g.presupuesto_estimado, str):
        try:
            presu = float(g.presupuesto_estimado.replace(",", "."))
        except Exception:
            presu = None

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
                sec_id, (g.estado or "enviado"),
                g.fecha_pedido, g.fecha_desde, g.fecha_hasta,
                presu, g.observaciones, g.created_by_username
            ))
            ped = cur.fetchone()
            pedido_id = ped["id"]

            # 3) Ámbito
            cur.execute("""
                INSERT INTO public.pedido_ambito (pedido_id, tipo)
                VALUES (%s, %s)
            """, (pedido_id, tipo_ambito_db))

            # (Opcional) detalles de obra/escuelas si vinieran en `especiales`
            if ambitoIncluido == "obra" and body.especiales and body.especiales.get("obra_nombre"):
                cur.execute("""
                    INSERT INTO public.ambito_obra (pedido_id, nombre_obra)
                    VALUES (%s, %s)
                """, (pedido_id, body.especiales["obra_nombre"]))
            elif ambitoIncluido == "mantenimientodeescuelas" and body.especiales and body.especiales.get("escuela"):
                cur.execute("""
                    INSERT INTO public.ambito_mant_escuela (pedido_id, escuela)
                    VALUES (%s, %s)
                """, (pedido_id, body.especiales["escuela"]))

            # 4) Módulo según modulo_draft.modulo
            m = md.modulo
            p = md.payload or {}

            if m == "servicios":
                if p.get("tipo_profesional"):
                    cur.execute("""
                        INSERT INTO public.pedido_servicios
                          (pedido_id, tipo_servicio, tipo_profesional, dia_desde, dia_hasta)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (pedido_id, "profesionales", p.get("tipo_profesional"), p.get("dia_desde"), p.get("dia_hasta")))
                else:
                    cur.execute("""
                        INSERT INTO public.pedido_servicios
                          (pedido_id, tipo_servicio, servicio_requerido, destino_servicio)
                        VALUES (%s, %s, %s, %s)
                    """, (pedido_id, "otros", p.get("servicio_requerido"), p.get("destino_servicio")))

            elif m == "alquiler":
                cur.execute("""
                    INSERT INTO public.pedido_alquiler
                      (pedido_id, categoria, uso_edificio, ubicacion_edificio, uso_maquinaria, tipo_maquinaria,
                       requiere_combustible, requiere_chofer, cronograma_desde, cronograma_hasta,
                       horas_por_dia, que_alquilar, detalle_uso)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    pedido_id,
                    p.get("categoria") or "otros",
                    p.get("uso_edificio"), p.get("ubicacion_edificio"),
                    p.get("uso_maquinaria"), p.get("tipo_maquinaria"),
                    p.get("requiere_combustible"), p.get("requiere_chofer"),
                    p.get("cronograma_desde"), p.get("cronograma_hasta"),
                    p.get("horas_por_dia"), p.get("que_alquilar"), p.get("detalle_uso"),
                ))

            elif m == "adquisicion":
                cur.execute("""
                    INSERT INTO public.pedido_adquisicion (pedido_id, proposito, modo_adquisicion)
                    VALUES (%s, %s, %s)
                """, (pedido_id, p.get("proposito"), p.get("modo_adquisicion") or "uno"))
                items: List[Dict[str, Any]] = p.get("items") or []
                if items:
                    cur.executemany("""
                        INSERT INTO public.pedido_adquisicion_item
                          (pedido_id, descripcion, cantidad, unidad, precio_unitario, total)
                        VALUES (%s, %s, %s, %s, %s, COALESCE(%s, (COALESCE(%s,0) * COALESCE(%s,1))))
                    """, [
                        (
                            pedido_id,
                            it.get("descripcion"),
                            it.get("cantidad") or 1,
                            it.get("unidad"),
                            it.get("precio_unitario"),
                            None,
                            it.get("precio_unitario"), it.get("cantidad") or 1
                        )
                        for it in items
                    ])

            elif m == "reparacion":
                cur.execute("""
                    INSERT INTO public.pedido_reparacion
                      (pedido_id, tipo_reparacion, unidad_reparar, que_reparar, detalle_reparacion)
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    pedido_id,
                    p.get("tipo_reparacion") or "otros",
                    p.get("unidad_reparar"),
                    p.get("que_reparar"),
                    p.get("detalle_reparacion"),
                ))

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
# Anexos (Supabase Storage) — SOLO presupuestos y anexo de obra (sin transiciones)
# =========================
def _sb_object_path(pedido_id: int, tipo_doc: str, filename: str) -> str:
    safe = (filename or "archivo.pdf").replace("/", "_").replace("\\", "_").strip()
    return f"pedido_{pedido_id}/{tipo_doc}/{uuid4()}_{safe}"

@router.post("/{pedido_id}/archivos")
async def upload_archivo(
    pedido_id: int,
    tipo_doc: str = Form(...),           # 'presupuesto_1' | 'presupuesto_2' | 'anexo1_obra'
    archivo: UploadFile = File(...),     # clave "archivo" en multipart/form-data
):
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=500, detail="Faltan SUPABASE_URL o SUPABASE_SERVICE_ROLE_KEY en el backend")
    if tipo_doc not in ALLOWED_TIPO_DOC:
        raise HTTPException(status_code=400, detail=f"tipo_doc inválido: {tipo_doc}. Permitidos: {', '.join(sorted(ALLOWED_TIPO_DOC))}")
    if not archivo.filename:
        raise HTTPException(status_code=400, detail="Falta nombre de archivo")
    mime = archivo.content_type or "application/pdf"
    if mime != "application/pdf":
        raise HTTPException(status_code=415, detail=f"Solo se aceptan PDF. Recibido: {mime}")

    data: bytes = await archivo.read()
    if not data:
        raise HTTPException(status_code=400, detail="El archivo llegó vacío (0 bytes)")

    # Verificar pedido
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT 1 FROM public.pedido WHERE id = %s;", (pedido_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Pedido no encontrado")

    # Subir a Storage
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

    # Guardar metadatos (sin transiciones de estado)
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

# Compat: endpoint anterior (clave "file"), delega al nuevo (solo anexo1_obra)
@router.post("/{pedido_id}/archivos/anexo1_obra")
async def upload_anexo1_obra(pedido_id: int, file: UploadFile = File(...)):
    return await upload_archivo(pedido_id=pedido_id, tipo_doc="anexo1_obra", archivo=file)
