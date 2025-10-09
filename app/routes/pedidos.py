from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from psycopg.rows import dict_row
from app.db import get_conn
from app.schemas import (
    PedidoServiciosIn, PedidoAlquilerIn, PedidoAdquisicionIn, PedidoReparacionIn,
    PedidoObrasIn, PedidoEscuelasIn, PedidoResult
)

router = APIRouter(prefix="/pedidos", tags=["pedidos"])

# -------- util --------
def insert_pedido(cur, modulo: str, body) -> dict:
    sql = """
    insert into public.pedido(
        modulo, estado, secretaria_id, fecha_pedido, fecha_desde, fecha_hasta,
        presupuesto_estimado, observaciones, created_by
    )
    values (%s, 'enviado', %s, current_date, %s, %s, %s, %s, %s)
    returning id, numero;
    """
    cur.execute(sql, (
        modulo, body.secretaria_id, body.fecha_desde, body.fecha_hasta,
        body.presupuesto_estimado, body.observaciones, body.created_by
    ))
    return cur.fetchone()

# -------- crear por módulo --------
@router.post("/servicios", response_model=PedidoResult)
def crear_servicios(body: PedidoServiciosIn):
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        try:
            ped = insert_pedido(cur, "servicios", body)
            cur.execute("""
              insert into public.mod_servicios(
                pedido_id, tipo_servicio, detalle_mantenimiento, tipo_profesional, dia_desde, dia_hasta
              ) values (%s, %s, %s, %s, %s, %s)
            """, (ped["id"], body.tipo_servicio, body.detalle_mantenimiento, body.tipo_profesional,
                  body.dia_desde, body.dia_hasta))
            conn.commit()
            return ped
        except Exception as e:
            conn.rollback()
            raise HTTPException(400, detail=str(e))

@router.post("/alquiler", response_model=PedidoResult)
def crear_alquiler(body: PedidoAlquilerIn):
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        try:
            ped = insert_pedido(cur, "alquiler", body)
            cur.execute("""
              insert into public.mod_alquiler(
                pedido_id, categoria, uso_edificio, ubicacion_edificio, uso_maquinaria, tipo_maquinaria,
                requiere_combustible, requiere_chofer, cronograma_desde, cronograma_hasta, horas_por_dia,
                que_alquilar, detalle_uso
              ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (ped["id"], body.categoria, body.uso_edificio, body.ubicacion_edificio, body.uso_maquinaria,
                  body.tipo_maquinaria, body.requiere_combustible, body.requiere_chofer,
                  body.cronograma_desde, body.cronograma_hasta, body.horas_por_dia,
                  body.que_alquilar, body.detalle_uso))
            conn.commit()
            return ped
        except Exception as e:
            conn.rollback()
            raise HTTPException(400, detail=str(e))

@router.post("/adquisicion", response_model=PedidoResult)
def crear_adquisicion(body: PedidoAdquisicionIn):
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        try:
            ped = insert_pedido(cur, "adquisicion", body)
            cur.execute("""
              insert into public.mod_adquisicion(pedido_id, proposito, modo_adquisicion)
              values (%s, %s, %s)
            """, (ped["id"], body.proposito, body.modo_adquisicion))
            if body.items:
                cur.executemany("""
                  insert into public.mod_adquisicion_item(pedido_id, descripcion, cantidad, unidad, precio_unitario)
                  values (%s, %s, %s, %s, %s)
                """, [(ped["id"], it.descripcion, it.cantidad, it.unidad, it.precio_unitario) for it in body.items])
            conn.commit()
            return ped
        except Exception as e:
            conn.rollback()
            raise HTTPException(400, detail=str(e))

@router.post("/reparacion", response_model=PedidoResult)
def crear_reparacion(body: PedidoReparacionIn):
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        try:
            ped = insert_pedido(cur, "reparacion", body)
            cur.execute("""
              insert into public.mod_reparacion(
                pedido_id, tipo_reparacion, unidad_reparar, que_reparar, detalle_reparacion
              ) values (%s, %s, %s, %s, %s)
            """, (ped["id"], body.tipo_reparacion, body.unidad_reparar, body.que_reparar, body.detalle_reparacion))
            conn.commit()
            return ped
        except Exception as e:
            conn.rollback()
            raise HTTPException(400, detail=str(e))

@router.post("/obras", response_model=PedidoResult)
def crear_obras(body: PedidoObrasIn):
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        try:
            ped = insert_pedido(cur, "obras", body)
            cur.execute("""
              insert into public.esp_obras(
                pedido_id, nombre_obra, ubicacion, detalle, presupuesto_obra, fecha_inicio, fecha_fin
              ) values (%s, %s, %s, %s, %s, %s, %s)
            """, (ped["id"], body.nombre_obra, body.ubicacion, body.detalle,
                  body.presupuesto_obra, body.fecha_inicio, body.fecha_fin))
            conn.commit()
            return ped
        except Exception as e:
            conn.rollback()
            raise HTTPException(400, detail=str(e))

@router.post("/mantenimientodeescuelas", response_model=PedidoResult)
def crear_mant_escuelas(body: PedidoEscuelasIn):
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        try:
            ped = insert_pedido(cur, "mantenimientodeescuelas", body)
            cur.execute("""
              insert into public.esp_mant_escuelas(
                pedido_id, escuela, ubicacion, necesidad, fecha_desde, fecha_hasta, detalle
              ) values (%s, %s, %s, %s, %s, %s, %s)
            """, (ped["id"], body.escuela, body.ubicacion, body.necesidad,
                  body.fecha_desde, body.fecha_hasta, body.detalle))
            conn.commit()
            return ped
        except Exception as e:
            conn.rollback()
            raise HTTPException(400, detail=str(e))

# -------- listar / detalle --------
@router.get("", summary="Lista (vista)")
def listar_pedidos(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                   modulo: Optional[str] = None, estado: Optional[str] = None):
    base = "select * from public.v_pedidos_list"
    where, params = [], []
    if modulo:
        where.append("modulo = %s"); params.append(modulo)
    if estado:
        where.append("estado = %s"); params.append(estado)
    if where:
        base += " where " + " and ".join(where)
    base += " order by id desc limit %s offset %s"
    params.extend([limit, offset])

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(base, params)
        return cur.fetchall()

@router.get("/{pedido_id}", summary="Detalle (vista)")
def detalle_pedido(pedido_id: int):
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from public.v_pedido_detalle where id = %s", (pedido_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, detail="Pedido no encontrado")
        return row
