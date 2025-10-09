# app/schemas.py
from typing import Optional, Literal
from pydantic import BaseModel, Field, condecimal

# -------------------------------------------------------------------
# Tipos comunes
# -------------------------------------------------------------------
Modulo = Literal[
    "servicios",
    "alquiler",
    "adquisicion",
    "reparacion",
    "obras",
    "mantenimientodeescuelas",
]

# -------------------------------------------------------------------
# Auth
# -------------------------------------------------------------------
class LoginIn(BaseModel):
    username: str
    password: str


class LoginOut(BaseModel):
    user_id: str
    nombre: Optional[str] = None
    secretaria_id: Optional[int] = None
    secretaria: Optional[str] = None


# -------------------------------------------------------------------
# Pedido (base)
# -------------------------------------------------------------------
class PedidoBase(BaseModel):
    secretaria_id: int
    observaciones: Optional[str] = None
    fecha_desde: Optional[str] = None  # ISO date string (yyyy-mm-dd)
    fecha_hasta: Optional[str] = None  # ISO date string
    presupuesto_estimado: Optional[condecimal(max_digits=14, decimal_places=2)] = None
    created_by: Optional[str] = None  # uuid string (puede ser null si no usás auth)


# -------------------------------------------------------------------
# Módulo: Servicios
# -------------------------------------------------------------------
class PedidoServiciosIn(PedidoBase):
    tipo_servicio: Literal["mantenimiento", "profesionales"]
    detalle_mantenimiento: Optional[str] = None
    tipo_profesional: Optional[str] = None
    dia_desde: Optional[str] = None  # ISO date
    dia_hasta: Optional[str] = None  # ISO date


# -------------------------------------------------------------------
# Módulo: Alquiler
# -------------------------------------------------------------------
class PedidoAlquilerIn(PedidoBase):
    categoria: Literal["edificio", "maquinaria", "otros"]
    uso_edificio: Optional[str] = None
    ubicacion_edificio: Optional[str] = None
    uso_maquinaria: Optional[str] = None
    tipo_maquinaria: Optional[str] = None
    requiere_combustible: Optional[bool] = None
    requiere_chofer: Optional[bool] = None
    cronograma_desde: Optional[str] = None  # ISO date
    cronograma_hasta: Optional[str] = None  # ISO date
    horas_por_dia: Optional[condecimal(max_digits=6, decimal_places=2)] = None
    que_alquilar: Optional[str] = None
    detalle_uso: Optional[str] = None


# -------------------------------------------------------------------
# Módulo: Adquisición
# -------------------------------------------------------------------
class Item(BaseModel):
    descripcion: str
    cantidad: condecimal(max_digits=12, decimal_places=3) = Field(..., example="1")
    unidad: Optional[str] = None
    precio_unitario: Optional[condecimal(max_digits=14, decimal_places=2)] = None


class PedidoAdquisicionIn(PedidoBase):
    proposito: Optional[str] = None
    modo_adquisicion: Literal["uno", "muchos"] = "uno"
    # Pydantic v2: usar list[...] + Field(min_length=0)
    items: list[Item] = Field(default_factory=list, min_length=0)


# -------------------------------------------------------------------
# Módulo: Reparación
# -------------------------------------------------------------------
class PedidoReparacionIn(PedidoBase):
    tipo_reparacion: Literal["maquinaria", "otros"]
    unidad_reparar: Optional[str] = None  # cuando es 'maquinaria'
    que_reparar: Optional[str] = None     # cuando es 'otros'
    detalle_reparacion: Optional[str] = None


# -------------------------------------------------------------------
# Módulo: Obras (especial)
# -------------------------------------------------------------------
class PedidoObrasIn(PedidoBase):
    nombre_obra: str
    ubicacion: Optional[str] = None
    detalle: Optional[str] = None
    presupuesto_obra: Optional[condecimal(max_digits=14, decimal_places=2)] = None
    fecha_inicio: Optional[str] = None  # ISO date
    fecha_fin: Optional[str] = None     # ISO date


# -------------------------------------------------------------------
# Módulo: Mantenimiento de Escuelas (especial)
# -------------------------------------------------------------------
class PedidoEscuelasIn(PedidoBase):
    escuela: str
    ubicacion: Optional[str] = None
    necesidad: Optional[str] = None
    detalle: Optional[str] = None
    # fechas opcionales ya heredadas: fecha_desde / fecha_hasta


# -------------------------------------------------------------------
# Respuesta genérica al crear pedido
# -------------------------------------------------------------------
class PedidoResult(BaseModel):
    id: int
    numero: str
