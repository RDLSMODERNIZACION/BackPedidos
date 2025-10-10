# app/routes/pedidos.py
from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel, Field
from typing import Optional, Literal, List, Union, Dict, Any
from psycopg.rows import dict_row
from app.db import get_conn
from datetime import date

router = APIRouter(prefix="/pedidos", tags=["pedidos"])

# ---------------------------
# Pydantic In
# ---------------------------

class GeneralIn(BaseModel):
    secretaria: str
    estado: Literal["borrador","enviado","en_revision","aprobado","rechazado","cerrado"] = "enviado"
    fecha_pedido: Optional[date] = None
    fecha_desde: Optional[date] = None
    fecha_hasta: Optional[date] = None
    presupuesto_estimado: Optional[float] = None
    observaciones: Optional[str] = None
    created_by_username: Optional[str] = None

# Ámbitos
AmbitoTipo = Literal["ninguno","obra","mantenimientodeescuelas"]

class AmbitoObraIn(BaseModel):
    obra_nombre: str = Field(..., description="Nombre de la obra")
    ubicacion: Optional[str] = None
    detalle: Optional[str] = None
    presupuesto_obra: Optional[float] = None
    fecha_inicio: Optional[date] = None
    fecha_fin: Optional[date] = None
    es_nueva: Optional[bool] = True
    obra_existente_ref: Optional[str] = None

class AmbitoEscuelaIn(BaseModel):
    escuela: str = Field(..., description="Nombre de la escuela")
    ubicacion: Optional[str] = None
    necesidad: Optional[str] = None
    fecha_desde: Optional[date] = None
    fecha_hasta: Optional[date] = None
    detalle: Optional[str] = None

class AmbitoIn(BaseModel):
    tipo: AmbitoTipo
    obra: Optional[AmbitoObraIn] = None
    escuelas: Optional[AmbitoEscuelaIn] = None

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

# ---------------------------
# Helpers
# ---------------------------

def _one_or_none(rows: list[dict]) -> Optional[dict]:
    return rows[0] if rows else None

def _lookup_secretaria_id(cur, nombre: str) -> int:
    cur.execute("SELECT id FROM public.secretaria WHERE nombre=%s", (nombre,))
    row = _one_or_none(cur.fetchall())
    if not row:
        raise HTTPException(status_code=400, detail=f"Secretaría no encontrada: {nombre}")
    return row["id"]

def _map_ambito_ui_to_db(t: AmbitoTipo) -> str:
    # UI: 'ninguno'|'obra'|'mantenimientodeescuelas' -> DB enum: 'general'|'obra'|'mant_escuela'
    return {"ninguno":"general","obra":"obra","mantenimientodeescuelas":"mant_escuela"}[t]

# ---------------------------
# POST /pedidos
# ---------------------------

@router.post("", status_code=201)
def create_pedido_full(payload: PedidoCreate) -> Dict[str, Any]:
    g = payload.generales
    a = payload.ambito
    m = payload.modulo

    # Validaciones de consistencia según tipo_ui
    if a.tipo == "obra" and not a.obra:
        raise HTTPException(status_code=400, detail="Falta 'ambito.obra' para tipo=obra.")
    if a.tipo == "mantenimientodeescuelas" and not a.escuelas:
        raise HTTPException(status_code=400, detail="Falta 'ambito.escuelas' para tipo=mantenimientodeescuelas.")

    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            # 1) Secretaria
            sec_id = _lookup_secretaria_id(cur, g.secretaria)

            # 2) Pedido (header)
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

            # 3) ÁMBITO
            tipo_db = _map_ambito_ui_to_db(a.tipo)
            cur.execute("""
                INSERT INTO public.pedido_ambito (pedido_id, tipo)
                VALUES (%s, %s)
            """, (pedido_id, tipo_db))

            if a.tipo == "obra":
                ao = a.obra  # type: ignore
                cur.execute("""
                  INSERT INTO public.ambito_obra
                    (pedido_id, nombre_obra, ubicacion, detalle, presupuesto_obra,
                     fecha_inicio, fecha_fin, es_nueva, obra_existente_ref)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,COALESCE(%s,true),%s)
                """, (
                    pedido_id, ao.obra_nombre, ao.ubicacion, ao.detalle, ao.presupuesto_obra,
                    ao.fecha_inicio, ao.fecha_fin, ao.es_nueva, ao.obra_existente_ref
                ))
            elif a.tipo == "mantenimientodeescuelas":
                am = a.escuelas  # type: ignore
                cur.execute("""
                  INSERT INTO public.ambito_mant_escuela
                    (pedido_id, escuela, ubicacion, necesidad, fecha_desde, fecha_hasta, detalle)
                  VALUES (%s,%s,%s,%s,%s,%s,%s)
                """, (
                    pedido_id, am.escuela, am.ubicacion, am.necesidad, am.fecha_desde, am.fecha_hasta, am.detalle
                ))

            # 4) MÓDULO
            if m.tipo == "servicios":
                cur.execute("""
                  INSERT INTO public.pedido_servicios
                    (pedido_id, tipo_servicio, detalle_mantenimiento, tipo_profesional, dia_desde, dia_hasta)
                  VALUES (%s,%s,%s,%s,%s,%s)
                """, (
                    pedido_id, m.tipo_servicio, m.detalle_mantenimiento, m.tipo_profesional, m.dia_desde, m.dia_hasta
                ))

            elif m.tipo == "alquiler":
                cur.execute("""
                  INSERT INTO public.pedido_alquiler
                    (pedido_id, categoria, uso_edificio, ubicacion_edificio,
                     uso_maquinaria, tipo_maquinaria, requiere_combustible, requiere_chofer,
                     cronograma_desde, cronograma_hasta, horas_por_dia,
                     que_alquilar, detalle_uso)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    pedido_id, m.categoria, m.uso_edificio, m.ubicacion_edificio,
                    m.uso_maquinaria, m.tipo_maquinaria, m.requiere_combustible, m.requiere_chofer,
                    m.cronograma_desde, m.cronograma_hasta, m.horas_por_dia,
                    m.que_alquilar, m.detalle_uso
                ))

            elif m.tipo == "adquisicion":
                cur.execute("""
                  INSERT INTO public.pedido_adquisicion
                    (pedido_id, proposito, modo_adquisicion)
                  VALUES (%s,%s,%s)
                """, (pedido_id, m.proposito, m.modo_adquisicion))
                # items
                if m.items:
                    # bulk insert
                    rows = []
                    for it in m.items:
                        total = None
                        if it.precio_unitario is not None:
                            total = (it.cantidad or 1) * it.precio_unitario
                        rows.append((
                            pedido_id, it.descripcion, it.cantidad or 1, it.unidad, it.precio_unitario, total
                        ))
                    cur.executemany("""
                      INSERT INTO public.pedido_adquisicion_item
                        (pedido_id, descripcion, cantidad, unidad, precio_unitario, total)
                      VALUES (%s,%s,%s,%s,%s,%s)
                    """, rows)

            elif m.tipo == "reparacion":
                cur.execute("""
                  INSERT INTO public.pedido_reparacion
                    (pedido_id, tipo_reparacion, unidad_reparar, que_reparar, detalle_reparacion)
                  VALUES (%s,%s,%s,%s,%s)
                """, (
                    pedido_id, m.tipo_reparacion, m.unidad_reparar, m.que_reparar, m.detalle_reparacion
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

# ---------------------------
# POST /pedidos/{id}/archivos/anexo1_obra
# ---------------------------

@router.post("/{pedido_id}/archivos/anexo1_obra")
async def upload_anexo1_obra(pedido_id: int, file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="El anexo debe ser PDF.")
    storage_path = f"uploads/pedido_{pedido_id}/anexo1_obra.pdf"

    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
              INSERT INTO public.pedido_archivo
                (pedido_id, storage_path, file_name, content_type, bytes, tipo_doc)
              VALUES (%s,%s,%s,%s,%s,'anexo1_obra')
              ON CONFLICT (pedido_id, tipo_doc) DO UPDATE
                SET storage_path = EXCLUDED.storage_path,
                    file_name    = EXCLUDED.file_name,
                    content_type = EXCLUDED.content_type,
                    bytes        = EXCLUDED.bytes,
                    created_at   = now()
              RETURNING id
            """, (
                pedido_id, storage_path, file.filename, file.content_type or "application/pdf", 0
            ))
            row = cur.fetchone()
            return {"ok": True, "archivo_id": row["id"], "storage_path": storage_path}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"upload_error: {e}")

# ---------------------------
# GET /pedidos/detail/{pedido_id}  (detalle para pestaña Info)
# ---------------------------

@router.get("/detail/{pedido_id}")
def get_pedido_detail(pedido_id: int) -> Dict[str, Any]:
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
                  pr.nombre AS solicitante
                FROM public.pedido p
                JOIN public.secretaria s ON s.id = p.secretaria_id
                LEFT JOIN public.perfil pr ON pr.user_id = p.created_by
                WHERE p.id = %s
            """, (pedido_id,))
            base = _one_or_none(cur.fetchall())
            if not base:
                raise HTTPException(status_code=404, detail="Pedido no encontrado")

            out: Dict[str, Any] = { **base, "ambito": None, "modulo": None }

            # Ámbito
            cur.execute("SELECT tipo::text AS tipo_db FROM public.pedido_ambito WHERE pedido_id=%s", (pedido_id,))
            amb = _one_or_none(cur.fetchall())
            tipo_db = amb["tipo_db"] if amb else "general"

            if tipo_db == "obra":
                cur.execute("""
                    SELECT nombre_obra, ubicacion, detalle, presupuesto_obra,
                           fecha_inicio, fecha_fin, es_nueva, obra_existente_ref
                    FROM public.ambito_obra WHERE pedido_id=%s
                """, (pedido_id,))
                row = _one_or_none(cur.fetchall()) or {}
                out["ambito"] = {
                    "tipo": "obra",
                    "obra": {
                        "obra_nombre": row.get("nombre_obra"),
                        "ubicacion": row.get("ubicacion"),
                        "detalle": row.get("detalle"),
                        "presupuesto_obra": row.get("presupuesto_obra"),
                        "fecha_inicio": row.get("fecha_inicio"),
                        "fecha_fin": row.get("fecha_fin"),
                        "es_nueva": row.get("es_nueva"),
                        "obra_existente_ref": row.get("obra_existente_ref"),
                    }
                }
            elif tipo_db == "mant_escuela":
                cur.execute("""
                    SELECT escuela, ubicacion, necesidad, fecha_desde, fecha_hasta, detalle
                    FROM public.ambito_mant_escuela WHERE pedido_id=%s
                """, (pedido_id,))
                row = _one_or_none(cur.fetchall()) or {}
                out["ambito"] = {
                    "tipo": "mantenimientodeescuelas",
                    "escuelas": {
                        "escuela": row.get("escuela"),
                        "ubicacion": row.get("ubicacion"),
                        "necesidad": row.get("necesidad"),
                        "fecha_desde": row.get("fecha_desde"),
                        "fecha_hasta": row.get("fecha_hasta"),
                        "detalle": row.get("detalle"),
                    }
                }
            else:
                out["ambito"] = {"tipo": "ninguno"}

            # Módulo (uno solo por pedido)
            # Servicios
            cur.execute("""
                SELECT tipo_servicio, detalle_mantenimiento, tipo_profesional, dia_desde, dia_hasta
                FROM public.pedido_servicios WHERE pedido_id=%s
            """, (pedido_id,))
            row = _one_or_none(cur.fetchall())
            if row:
                out["modulo"] = {"tipo":"servicios", **row}
                return out

            # Alquiler
            cur.execute("""
                SELECT categoria, uso_edificio, ubicacion_edificio,
                       uso_maquinaria, tipo_maquinaria,
                       requiere_combustible, requiere_chofer,
                       cronograma_desde, cronograma_hasta, horas_por_dia,
                       que_alquilar, detalle_uso
                FROM public.pedido_alquiler WHERE pedido_id=%s
            """, (pedido_id,))
            row = _one_or_none(cur.fetchall())
            if row:
                out["modulo"] = {"tipo":"alquiler", **row}
                return out

            # Adquisición + items
            cur.execute("""
                SELECT proposito, modo_adquisicion
                FROM public.pedido_adquisicion WHERE pedido_id=%s
            """, (pedido_id,))
            head = _one_or_none(cur.fetchall())
            if head:
                cur.execute("""
                    SELECT descripcion, cantidad, unidad, precio_unitario, total
                    FROM public.pedido_adquisicion_item
                    WHERE pedido_id=%s ORDER BY id
                """, (pedido_id,))
                items = cur.fetchall()
                out["modulo"] = {"tipo":"adquisicion", **head, "items": items}
                return out

            # Reparación
            cur.execute("""
                SELECT tipo_reparacion, unidad_reparar, que_reparar, detalle_reparacion
                FROM public.pedido_reparacion
                WHERE pedido_id=%s
            """, (pedido_id,))
            row = _one_or_none(cur.fetchall())
            if row:
                out["modulo"] = {"tipo":"reparacion", **row}
                return out

            out["modulo"] = None
            return out
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"detalle_error: {e}")
