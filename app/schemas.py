from typing import Optional, List, Literal
from pydantic import BaseModel, Field, conlist, condecimal

Modulo = Literal["servicios","alquiler","adquisicion","reparacion","obras","mantenimientodeescuelas"]

class LoginIn(BaseModel):
    username: str
    password: str

class LoginOut(BaseModel):
    user_id: str
    nombre: Optional[str]
    secretaria_id: Optional[int]
    secretaria: Optional[str]

class PedidoBase(BaseModel):
    secretaria_id: int
    observaciones: Optional[str] = None
    fecha_desde: Optional[str] = None   # ISO date
    fecha_hasta: Optional[str] = None   # ISO date
    presupuesto_estimado: Optional[condecimal(max_digits=14, decimal_places=2)] = None
    created_by: Optional[str] = None    # uuid string o null

class PedidoServiciosIn(PedidoBase):
    tipo_servicio: Literal["mantenimiento","profesionales"]
    detalle_mantenimiento: Optional[str] = None
    tipo_profesional: Optional[str] = None
    dia_desde: Optional[str] = None
    dia_hasta: Optional[str] = None

class PedidoAlquilerIn(PedidoBase):
    categoria: Literal["edificio","maquinaria","otros"]
    uso_edificio: Optional[str] = None
    ubicacion_edificio: Optional[str] = None
    uso_maquinaria: Optional[str] = None
    tipo_maquinaria: Optional[str] = None
    requiere_combustible: Optional[bool] = None
    requiere_chofer: Optional[bool] = None
    cronograma_desde: Optional[str] = None
    cronograma_hasta: Optional[str] = None
    horas_por_dia: Optional[condecimal(max_digits=6, decimal_places=2)] = None
    que_alquilar: Optional[str] = None
    detalle_uso: Optional[str] = None

class Item(BaseModel):
    descripcion: str
    cantidad: condecimal(max_digits=12, decimal_places=3) = Field(..., example="1")
    unidad: Optional[str] = None
    precio_unitario: Optional[condecimal(max_digits=14, decimal_places=2)] = None

class PedidoAdquisicionIn(PedidoBase):
    proposito: Optional[str] = None
    modo_adquisicion: Literal["uno","muchos"] = "uno"
    items: conlist(Item, min_items=0) = []

class PedidoReparacionIn(PedidoBase):
    tipo_reparacion: Literal["maquinaria","otros"]
    unidad_reparar: Optional[str] = None
    que_reparar: Optional[str] = None
    detalle_reparacion: Optional[str] = None

class PedidoObrasIn(PedidoBase):
    nombre_obra: str
    ubicacion: Optional[str] = None
    detalle: Optional[str] = None
    presupuesto_obra: Optional[condecimal(max_digits=14, decimal_places=2)] = None
    fecha_inicio: Optional[str] = None
    fecha_fin: Optional[str] = None

class PedidoEscuelasIn(PedidoBase):
    escuela: str
    ubicacion: Optional[str] = None
    necesidad: Optional[str] = None
    detalle: Optional[str] = None

class PedidoResult(BaseModel):
    id: int
    numero: str
