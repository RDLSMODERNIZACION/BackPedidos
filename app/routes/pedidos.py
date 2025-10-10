# app/routes/ui.py
from fastapi import APIRouter, HTTPException
from psycopg.rows import dict_row
from typing import Any, Dict
from app.db import get_conn

router = APIRouter(prefix="/ui", tags=["ui"])

def _one(rows: list[dict]) -> dict | None:
    return rows[0] if rows else None

def _map_ambito_db_to_ui(t: str | None) -> str:
    # DB enum: 'general' | 'obra' | 'mant_escuela'
    # UI:      'ninguno' | 'obra' | 'mantenimientodeescuelas'
    if t == "obra":
        return "obra"
    if t == "mant_escuela":
        return "mantenimientodeescuelas"
    return "ninguno"

@router.get("/pedidos/{pedido_id}")
def ui_pedido_detalle(pedido_id: int) -> Dict[str, Any]:
    """
    Devuelve generales + ambiente + módulo para un pedido.
    Estructura:
    {
      id, numero, estado, secretaria, solicitante, creado,
      fecha_pedido, fecha_desde, fecha_hasta, presupuesto_estimado, observaciones,
      ambito: { tipo: "obra"|"mantenimientodeescuelas"|"ninguno", obra?: {...}, escuelas?: {...} },
      modulo: { tipo: "servicios"|"alquiler"|"adquisicion"|"reparacion", ... } | null
    }
    """
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            # ---------- Generales ----------
            cur.execute("""
                SELECT
                  p.id,
                  p.numero,
                  p.estado,
                  p.fecha_pedido,
                  p.fecha_desde,
                  p.fecha_hasta,
                  p.presupuesto_estimado,
                  p.observaciones,
                  p.created_at AS creado,
                  s.nombre     AS secretaria,
                  pr.nombre    AS solicitante
                FROM public.pedido p
                JOIN public.secretaria s ON s.id = p.secretaria_id
                LEFT JOIN public.perfil pr ON pr.user_id = p.created_by
                WHERE p.id = %s
            """, (pedido_id,))
            base = _one(cur.fetchall())
            if not base:
                raise HTTPException(status_code=404, detail="Pedido no encontrado")

            out: Dict[str, Any] = {
                **base,
                "ambito": None,
                "modulo": None,
            }

            # ---------- ÁMBITO ----------
            cur.execute("""
                SELECT tipo::text AS tipo_db
                FROM public.pedido_ambito
                WHERE pedido_id = %s
            """, (pedido_id,))
            amb = _one(cur.fetchall())
            tipo_ui = _map_ambito_db_to_ui(amb["tipo_db"] if amb else None)

            if tipo_ui == "obra":
                cur.execute("""
                    SELECT nombre_obra, ubicacion, detalle, presupuesto_obra,
                           fecha_inicio, fecha_fin, es_nueva, obra_existente_ref
                    FROM public.ambito_obra
                    WHERE pedido_id = %s
                """, (pedido_id,))
                ao = _one(cur.fetchall()) or {}
                out["ambito"] = {
                    "tipo": "obra",
                    "obra": {
                        "obra_nombre": ao.get("nombre_obra"),
                        "ubicacion": ao.get("ubicacion"),
                        "detalle": ao.get("detalle"),
                        "presupuesto_obra": ao.get("presupuesto_obra"),
                        "fecha_inicio": ao.get("fecha_inicio"),
                        "fecha_fin": ao.get("fecha_fin"),
                        "es_nueva": ao.get("es_nueva"),
                        "obra_existente_ref": ao.get("obra_existente_ref"),
                    }
                }
            elif tipo_ui == "mantenimientodeescuelas":
                cur.execute("""
                    SELECT escuela, ubicacion, necesidad, fecha_desde, fecha_hasta, detalle
                    FROM public.ambito_mant_escuela
                    WHERE pedido_id = %s
                """, (pedido_id,))
                am = _one(cur.fetchall()) or {}
                out["ambito"] = {
                    "tipo": "mantenimientodeescuelas",
                    "escuelas": {
                        "escuela": am.get("escuela"),
                        "ubicacion": am.get("ubicacion"),
                        "necesidad": am.get("necesidad"),
                        "fecha_desde": am.get("fecha_desde"),
                        "fecha_hasta": am.get("fecha_hasta"),
                        "detalle": am.get("detalle"),
                    }
                }
            else:
                out["ambito"] = {"tipo": "ninguno"}

            # ---------- MÓDULO ----------
            # Servicios
            cur.execute("""
                SELECT tipo_servicio, detalle_mantenimiento, tipo_profesional, dia_desde, dia_hasta
                FROM public.pedido_servicios
                WHERE pedido_id = %s
            """, (pedido_id,))
            srv = _one(cur.fetchall())
            if srv:
                out["modulo"] = {
                    "tipo": "servicios",
                    **srv
                }
                return out  # corto aquí: sólo habrá un módulo por pedido

            # Alquiler
            cur.execute("""
                SELECT categoria, uso_edificio, ubicacion_edificio,
                       uso_maquinaria, tipo_maquinaria,
                       requiere_combustible, requiere_chofer,
                       cronograma_desde, cronograma_hasta, horas_por_dia,
                       que_alquilar, detalle_uso
                FROM public.pedido_alquiler
                WHERE pedido_id = %s
            """, (pedido_id,))
            alq = _one(cur.fetchall())
            if alq:
                out["modulo"] = {
                    "tipo": "alquiler",
                    **alq
                }
                return out

            # Adquisición + items
            cur.execute("""
                SELECT proposito, modo_adquisicion
                FROM public.pedido_adquisicion
                WHERE pedido_id = %s
            """, (pedido_id,))
            adq = _one(cur.fetchall())
            if adq:
                cur.execute("""
                    SELECT descripcion, cantidad, unidad, precio_unitario, total
                    FROM public.pedido_adquisicion_item
                    WHERE pedido_id = %s
                    ORDER BY id
                """, (pedido_id,))
                items = cur.fetchall()
                out["modulo"] = {
                    "tipo": "adquisicion",
                    **adq,
                    "items": items
                }
                return out

            # Reparación
            cur.execute("""
                SELECT tipo_reparacion, unidad_reparar, que_reparar, detalle_reparacion
                FROM public.pedido_reparacion
                WHERE pedido_id = %s
            """, (pedido_id,))
            rep = _one(cur.fetchall())
            if rep:
                out["modulo"] = {
                    "tipo": "reparacion",
                    **rep
                }
                return out

            # Si no hubo ningún módulo:
            out["modulo"] = None
            return out

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ui_pedido_detalle_error: {e}")
