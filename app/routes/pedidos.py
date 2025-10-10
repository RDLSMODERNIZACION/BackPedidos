# app/routes/pedidos.py
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
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

# ------------- POST /pedidos -------------

@router.post("", status_code=201)
def create_pedido_full(payload: PedidoCreate) -> Dict[str, Any]:
    g = payload.generales
    a = payload.ambito
    m = payload.modulo

    # Validación simple: un solo módulo permitido (ya lo asegura DB, pero avisamos bonito)
    # (La unión de Pydantic ya garantiza que venga solo uno)
    if a.tipo == "obra" and not a.obra:
        raise HTTPException(status_code=400, detail="Falta 'ambito.obra' para tipo=obra.")
    if a.tipo == "mantenimientodeescuelas" and not a.escuelas:
        raise HTTPException(status_code=400, detail="Falta 'ambito.escuelas' para tipo=mantenimientodeescuelas.")

    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            # 1) Secretaria
            sec_id = _lookup_secretaria_id(cur, g.secretaria)

            # 2) Encabezado pedido
            cur.execute("""
                INSERT INTO public.pedido (secretaria_id, estado, fecha_pedido, fecha_desde, fecha_hasta,
                                           presupuesto_estimado, observaciones, created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,
                        (SELECT user_id FROM public.perfil WHERE login_username=%s LIMIT 1))
                RETURNING id, numero, created_at, updated_at
            """, (sec_id, g.estado, g.fecha_pedido, g.fecha_desde, g.fecha_hasta,
                  g.presupuesto_estimado, g.observaciones, g.created_by_username))
            ped = cur.fetchone()
            pedido_id = ped["id"]

            # 3) ÁMBITO
            if a.tipo not in ("ninguno", "obra", "mantenimientodeescuelas"):
                raise HTTPException(status_code=400, detail="Ambito no válido.")

            cur.execute("""
                INSERT INTO public.pedido_ambito (pedido_id, tipo)
                VALUES (%s, %s)
            """, (pedido_id, a.tipo))

            if a.tipo == "obra":
                cur.execute("""
                  INSERT INTO public.ambito_obra (pedido_id, nombre_obra)
                  VALUES (%s, %s)
                """, (pedido_id, a.obra.obra_nombre))
            elif a.tipo == "mantenimientodeescuelas":
                cur.execute("""
                  INSERT INTO public.ambito_mant_escuela (pedido_id, escuela)
                  VALUES (%s, %s)
                """, (pedido_id, a.escuelas.escuela))

            # 4) MÓDULO (exclusivo)
            if m.tipo == "servicios":
                cur.execute("""
                  INSERT INTO public.pedido_servicios (pedido_id, tipo_servicio, detalle_mantenimiento,
                                                       tipo_profesional, dia_desde, dia_hasta)
                  VALUES (%s,%s,%s,%s,%s,%s)
                """, (pedido_id, m.tipo_servicio, m.detalle_mantenimiento,
                      m.tipo_profesional, m.dia_desde, m.dia_hasta))

            elif m.tipo == "alquiler":
                cur.execute("""
                  INSERT INTO public.pedido_alquiler (pedido_id, categoria, uso_edificio, ubicacion_edificio,
                                                      uso_maquinaria, tipo_maquinaria, requiere_combustible,
                                                      requiere_chofer, cronograma_desde, cronograma_hasta,
                                                      horas_por_dia, que_alquilar, detalle_uso)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (pedido_id, m.categoria, m.uso_edificio, m.ubicacion_edificio,
                      m.uso_maquinaria, m.tipo_maquinaria, m.requiere_combustible,
                      m.requiere_chofer, m.cronograma_desde, m.cronograma_hasta,
                      m.horas_por_dia, m.que_alquilar, m.detalle_uso))

            elif m.tipo == "adquisicion":
                cur.execute("""
                  INSERT INTO public.pedido_adquisicion (pedido_id, proposito, modo_adquisicion)
                  VALUES (%s,%s,%s)
                """, (pedido_id, m.proposito, m.modo_adquisicion))
                # Ítems
                if m.items:
                    cur.executemany("""
                      INSERT INTO public.pedido_adquisicion_item
                        (pedido_id, descripcion, cantidad, unidad, precio_unitario)
                      VALUES (%s,%s,%s,%s,%s)
                    """, [(pedido_id, it.descripcion, it.cantidad, it.unidad, it.precio_unitario)
                          for it in m.items])

            elif m.tipo == "reparacion":
                cur.execute("""
                  INSERT INTO public.pedido_reparacion (pedido_id, tipo_reparacion, unidad_reparar,
                                                        que_reparar, detalle_reparacion)
                  VALUES (%s,%s,%s,%s,%s)
                """, (pedido_id, m.tipo_reparacion, m.unidad_reparar,
                      m.que_reparar, m.detalle_reparacion))

            else:
                raise HTTPException(status_code=400, detail="Módulo no reconocido.")

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
    # Validaciones mínimas
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="El anexo debe ser PDF.")
    # Nota: acá iría tu subida a storage real (S3, Supabase, etc.)
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
