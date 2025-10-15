# app/routes/pedidos_acciones.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from psycopg.rows import dict_row
from app.db import get_conn

router = APIRouter(prefix="/pedidos", tags=["pedidos-acciones"])

class DecisionIn(BaseModel):
    decision: str                    # "aprobar" | "observar" | "rechazar"
    notes: Optional[str] = None
    changed_by: Optional[str] = None

class UpdatePedidoIn(BaseModel):
    observaciones: Optional[str] = None
    presupuesto_estimado: Optional[float] = None
    fecha_desde: Optional[str] = None     # YYYY-MM-DD
    fecha_hasta: Optional[str] = None
    modulo_payload: Optional[dict] = None
    ambito_payload: Optional[dict] = None

@router.post("/{pedido_id}/decision")
def decidir_pedido(pedido_id: int, body: DecisionIn):
    dec = (body.decision or "").lower().strip()
    if dec not in ("aprobar", "observar", "rechazar"):
        raise HTTPException(422, "decision debe ser: aprobar | observar | rechazar")

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT estado FROM public.pedido WHERE id=%s FOR UPDATE", (pedido_id,))
        p = cur.fetchone()
        if not p:
            raise HTTPException(404, "Pedido no encontrado")

        estado_actual = (p["estado"] or "").lower()
        estado_nuevo = estado_actual

        if dec == "aprobar":
            # reglas simples: desde enviado / en_revision / observado -> aprobado
            if estado_actual in ("aprobado","cerrado","area_pago","en_proceso"):
                raise HTTPException(409, f"No se puede aprobar desde estado '{estado_actual}'")
            estado_nuevo = "aprobado"

        elif dec == "observar":
            # observado siempre permitido
            if not body.notes or not body.notes.strip():
                raise HTTPException(422, "notes es requerido para observar")
            estado_nuevo = "observado"

        elif dec == "rechazar":
            if not body.notes or not body.notes.strip():
                raise HTTPException(422, "notes es requerido para rechazar")
            estado_nuevo = "rechazado"

        if estado_nuevo != estado_actual:
            cur.execute("UPDATE public.pedido SET estado=%s, updated_at=now() WHERE id=%s RETURNING estado",
                        (estado_nuevo, pedido_id))
            _ = cur.fetchone()
            cur.execute(
                "INSERT INTO public.pedido_historial (pedido_id, estado_anterior, estado_nuevo, motivo, changed_by) "
                "VALUES (%s,%s,%s,%s,%s)",
                (pedido_id, estado_actual, estado_nuevo, body.notes, body.changed_by or "ui"),
            )
            conn.commit()

        return {"ok": True, "estado": estado_nuevo}

@router.patch("/{pedido_id}")
def update_pedido(pedido_id: int, body: UpdatePedidoIn):
    # ediciones "seguras"
    sets = []
    vals = []
    if body.observaciones is not None:
        sets.append("observaciones = %s");       vals.append(body.observaciones)
    if body.presupuesto_estimado is not None:
        sets.append("presupuesto_estimado = %s"); vals.append(body.presupuesto_estimado)
    if body.fecha_desde is not None:
        sets.append("fecha_desde = %s");        vals.append(body.fecha_desde)
    if body.fecha_hasta is not None:
        sets.append("fecha_hasta = %s");        vals.append(body.fecha_hasta)
    if body.modulo_payload is not None:
        sets.append("modulo_payload = %s");     vals.append(body.modulo_payload)
    if body.ambito_payload is not None:
        sets.append("ambito_payload = %s");     vals.append(body.ambito_payload)

    if not sets:
        return {"ok": True}

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT 1 FROM public.pedido WHERE id=%s", (pedido_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Pedido no encontrado")

        sql = f"UPDATE public.pedido SET {', '.join(sets)}, updated_at = now() WHERE id = %s"
        vals.append(pedido_id)
        cur.execute(sql, tuple(vals))
        conn.commit()

    return {"ok": True}
