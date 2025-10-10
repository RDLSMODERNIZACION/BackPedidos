# app/routes/pedidos.py
from fastapi import APIRouter, HTTPException, UploadFile, File, Query
from pydantic import BaseModel, Field
from typing import Optional, Literal, List, Union, Dict, Any
from psycopg.rows import dict_row
from app.db import get_conn
from datetime import date

router = APIRouter(prefix="/pedidos", tags=["pedidos"])

# ------------- Pydantic In -------------

class GeneralIn(BaseModel):
    secretaria: str
    estado: Literal["borrador","enviado","en_revision","aprobado","rechazado","cerrado"] = "enviado"
    fecha_pedido: Optional[date] = None
    fecha_desde: Optional[date] = None
    fecha_hasta: Optional[date] = None
    presupuesto_estimado: Optional[float] = None
    observaciones: Optional[str] = None
    created_by_username: Optional[str] = None  # opcional, si querés registrar autor

# Ámbitos
AmbitoTipo = Literal["ninguno","obra","mantenimientodeescuelas"]

class AmbitoObraIn(BaseModel):
    obra_nombre: str = Field(..., description="Nombre de la obra (Obra)")

class AmbitoEscuelaIn(BaseModel):
    # Compat: por ahora seguimos recibiendo el nombre como texto
    escuela: str = Field(..., description="Nombre de la escuela (Mantenimiento de Escuelas)")

class AmbitoIn(BaseModel):
    tipo: AmbitoTipo
    obra: Optional[AmbitoObraIn] = None
    escuelas: Optional[AmbitoEscuelaIn] = None  # 'escuelas' para no chocar con palabra reservada

# Módulos
class ServiciosIn(BaseModel):
    tipo: Literal["servicios"]
    tipo_servicio: Literal["mantenimiento","profesionales"]
    detalle_mantenimiento: Optional[str] = None
    tipo_profesional: Optional[str] = None
    dia_desde: Optional[date] = None
    dia_hasta: Optional[date] = None

class AlquilerIn(BaseModel):
    tipo: Literal["alquiler"]
    categoria: Literal["edificio","maquinaria","otros"]
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
    modo_adquisicion: Literal["uno","muchos"] = "uno"
    items: List[AdqItemIn] = []

class ReparacionIn(BaseModel):
    tipo: Literal["reparacion"]
    tipo_reparacion: Literal["maquinaria","otros"]
    unidad_reparar: Optional[str] = None
    que_reparar: Optional[str] = None
    detalle_reparacion: Optional[str] = None

ModuloIn = Union[ServiciosIn, AlquilerIn, AdquisicionIn, ReparacionIn]

class PedidoCreate(BaseModel):
    generales: GeneralIn
    ambito: AmbitoIn
    modulo: ModuloIn

# ------------- Helpers -------------

def _one_or_none(rows: list[dict]) -> Optional[dict]:
    return rows[0] if rows else None

def _lookup_secretaria_id(cur, nombre: str) -> int:
    cur.execute("SELECT id FROM public.secretaria WHERE nombre=%s", (nombre,))
    row = _one_or_none(cur.fetchall())
    if not row:
        raise HTTPException(status_code=400, detail=f"Secretaría no encontrada: {nombre}")
    return row["id"]

# ========= Catálogo de Escuelas =========

class EscuelaIn(BaseModel):
    nombre: str = Field(..., min_length=2, description="Nombre visible de la escuela")
    ubicacion: Optional[str] = None
    activa: Optional[bool] = True

def _ensure_catalog_escuela(cur) -> None:
    """
    Crea public.catalog_escuela si no existe (robusto para primeros despliegues).
    """
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
    """
    Devuelve el catálogo de escuelas para un combo (id, nombre, ubicacion).
    Si la tabla public.catalog_escuela no existe aún, hace fallback a un DISTINCT
    sobre ambito_mant_escuela (sin IDs persistentes).
    """
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
        # Fallback si algo falla con catálogo
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
    """
    Crea o actualiza (upsert por nombre) una escuela en el catálogo.
    Devuelve {id, nombre, ubicacion, activa}.
    """
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

# ========= Catálogo de Obras =========

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
    """
    Lista de obras/lugares para combo. Si el catálogo no existe, hace fallback
    a nombres distintos en ambito_obra (sin IDs persistentes).
    """
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
    """
    Crea o actualiza (upsert por nombre) una obra/lugar en el catálogo.
    Devuelve {id, nombre, ubicacion, activa}.
    """
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

# ------------- POST /pedidos -------------

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

    # Consistencia por tipo
    if tipo_ui == "obra" and not a.obra:
        raise HTTPException(status_code=400, detail="Falta 'ambito.obra' para tipo=obra.")
    if tipo_ui == "mantenimientodeescuelas" and not a.escuelas:
        raise HTTPException(status_code=400, detail="Falta 'ambito.escuelas' para tipo=mantenimientodeescuelas.")

    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            # 1) Secretaria
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

            # 4) MÓDULO (deja tu inserción según corresponda)
            # ...

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

# ------------- POST /pedidos/{id}/archivos/anexo1_obra -------------

@router.post("/{pedido_id}/archivos/anexo1_obra")
async def upload_anexo1_obra(pedido_id: int, file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="El anexo debe ser PDF.")
    storage_path = f"uploads/pedido_{pedido_id}/anexo1_obra.pdf"

    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
              INSERT INTO public.pedido_archivo (pedido_id, storage_path, file_name, content_type, bytes, tipo_doc)
              VALUES (%s,%s,%s,%s,%s,'anexo1_obra')
              ON CONFLICT (pedido_id, tipo_doc) DO UPDATE
                SET storage_path = EXCLUDED.storage_path,
                    file_name    = EXCLUDED.file_name,
                    content_type = EXCLUDED.content_type,
                    bytes        = EXCLUDED.bytes,
                    created_at   = now()
              RETURNING id
            """, (pedido_id, storage_path, file.filename, file.content_type or "application/pdf", 0))
            row = cur.fetchone()
            return {"ok": True, "archivo_id": row["id"], "storage_path": storage_path}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"upload_error: {e}")
