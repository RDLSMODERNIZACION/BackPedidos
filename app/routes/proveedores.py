# app/routes/proveedores.py
# Endpoints para gestionar proveedores y su vínculo con pedidos.
# - Buscar por CUIT
# - Upsert de proveedor (crear/actualizar por CUIT, razón social, tel, email)
# - Upsert de teléfono
# - Agregar proveedor a expediente (pedido) por CUIT y rol
#
# Requisitos: fastapi, psycopg[binary,pool], pydantic
#   pip install fastapi "psycopg[binary,pool]" pydantic

import os
import re
from typing import Optional, Literal, List, Tuple
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from psycopg_pool import ConnectionPool

router = APIRouter(prefix="/proveedores", tags=["proveedores"])

# ---------- DB pool ----------
DB_URL = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
if not DB_URL:
    raise RuntimeError("Falta SUPABASE_DB_URL o DATABASE_URL")
POOL = ConnectionPool(DB_URL)

# ---------- Helpers ----------

CUIT_RE = re.compile(r"\d{8,11}")           # tolerante a 8..11 dígitos (algunos padrones locales)
E164_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")  # E.164

def _norm_cuit(cuit: str) -> str:
    """Normaliza CUIT/CUIL -> solo dígitos (sin guiones)."""
    return re.sub(r"\D", "", cuit or "")

def _to_e164(phone: str) -> str:
    """Acepta '+549...','549...','54...' o solo dígitos -> retorna '+<digits>'. No valida país."""
    if not phone:
        raise ValueError("telefono vacío")
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 8 or len(digits) > 15:
        raise ValueError("telefono inválido (8-15 dígitos internacionales)")
    return f"+{digits}"

def _fetch_proveedor_by_cuit(cur, cuit_norm: str):
    cur.execute("""
      SELECT id, cuit, razon_social, email_contacto, telefono, created_at, updated_at
      FROM public.proveedor
      WHERE cuit_num = %s OR REPLACE(cuit, '-', '') = %s
      LIMIT 1
    """, (cuit_norm, cuit_norm))
    return cur.fetchone()

def _ensure_proveedor(cur, cuit_norm: str, razon_social: Optional[str], email: Optional[str]):
    """Devuelve (id) creando proveedor si no existe. Usa razon_social fallback si es necesario."""
    row = _fetch_proveedor_by_cuit(cur, cuit_norm)
    if row:
        return row[0]
    rs = razon_social or f"Proveedor {cuit_norm}"
    cur.execute("""
      INSERT INTO public.proveedor (cuit, razon_social, email_contacto)
      VALUES (%s, %s, %s)
      RETURNING id
    """, (cuit_norm, rs, email))
    return cur.fetchone()[0]

# ---------- Modelos ----------

class ProviderOut(BaseModel):
    id: int
    cuit: str
    razon_social: Optional[str] = None
    email_contacto: Optional[str] = None
    telefono: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

class UpsertProveedorIn(BaseModel):
    cuit: str = Field(..., description="CUIT/CUIL con o sin guiones")
    razon_social: str = Field(..., description="Razón social del proveedor")
    telefono: Optional[str] = Field(None, description="E.164 o dígitos internacionales (se normaliza a +<digits>)")
    email_contacto: Optional[str] = None
    transfer_if_in_use: bool = Field(False, description="Si el teléfono ya está en otro proveedor, transferirlo aquí")

class UpsertPhoneIn(BaseModel):
    cuit: str = Field(..., description="CUIT/CUIL con o sin guiones")
    telefono: str = Field(..., description="E.164 o dígitos internacionales (se normaliza a +<digits>)")
    transfer_if_in_use: bool = Field(False, description="Si el número ya está en otro proveedor, transferirlo aquí")

class AddProveedorToPedidoIn(BaseModel):
    pedido_id: int = Field(..., ge=1)
    cuit: str = Field(..., description="CUIT/CUIL del proveedor")
    rol: Literal["invitado", "oferente", "adjudicatario", "consulta"] = "consulta"
    telefono: Optional[str] = Field(None, description="Si viene, se setea/actualiza el teléfono")
    razon_social: Optional[str] = None
    email_contacto: Optional[str] = None
    set_adjudicado: bool = Field(False, description="Si rol=adjudicatario y querés reflejarlo en pedido.adjudicado_a")

# ---------- Endpoints ----------

@router.get("/by-cuit/{cuit}", response_model=ProviderOut)
def get_by_cuit(cuit: str):
    """
    Trae proveedor por CUIT. Normaliza a dígitos (cuit_num).
    """
    cuit_norm = _norm_cuit(cuit)
    if not CUIT_RE.fullmatch(cuit_norm or ""):
        raise HTTPException(400, "CUIT inválido")
    with POOL.connection() as con, con.cursor() as cur:
        row = _fetch_proveedor_by_cuit(cur, cuit_norm)
        if not row:
            raise HTTPException(404, "Proveedor no encontrado")
        return ProviderOut(
            id=row[0], cuit=row[1], razon_social=row[2], email_contacto=row[3],
            telefono=row[4], created_at=str(row[5]) if row[5] else None, updated_at=str(row[6]) if row[6] else None
        )

@router.get("/search")
def search(q: str = Query(..., description="CUIT (parcial) o razón social (ilike)"), limit: int = 10):
    """
    Búsqueda simple por CUIT/razón social para autocompletar en el front.
    """
    q_digits = _norm_cuit(q)
    with POOL.connection() as con, con.cursor() as cur:
        if q_digits and len(q_digits) >= 4:
            cur.execute("""
              SELECT id, cuit, razon_social, email_contacto, telefono
              FROM public.proveedor
              WHERE cuit_num LIKE %s
                 OR REPLACE(cuit,'-','') LIKE %s
                 OR razon_social ILIKE %s
              ORDER BY updated_at DESC
              LIMIT %s
            """, (f"%{q_digits}%", f"%{q_digits}%", f"%{q}%", limit))
        else:
            cur.execute("""
              SELECT id, cuit, razon_social, email_contacto, telefono
              FROM public.proveedor
              WHERE razon_social ILIKE %s
              ORDER BY updated_at DESC
              LIMIT %s
            """, (f"%{q}%", limit))
        rows = cur.fetchall()
        return [
            {
                "id": r[0], "cuit": r[1], "razon_social": r[2],
                "email_contacto": r[3], "telefono": r[4]
            } for r in rows
        ]

@router.post("/upsert", response_model=ProviderOut)
def upsert_proveedor(body: UpsertProveedorIn):
    """
    Crea o actualiza un proveedor por CUIT.
    - Requiere: cuit, razon_social
    - Opcionales: telefono (normalizado a E.164), email_contacto
    - Si el teléfono está en otro proveedor:
        * transfer_if_in_use=false -> 409 Conflict
        * transfer_if_in_use=true  -> mueve el teléfono a este proveedor
    """
    cuit_norm = _norm_cuit(body.cuit)
    if not CUIT_RE.fullmatch(cuit_norm or ""):
        raise HTTPException(400, "CUIT inválido")

    tel: Optional[str] = None
    if body.telefono:
        try:
            tel = _to_e164(body.telefono)
        except Exception as e:
            raise HTTPException(400, f"Teléfono inválido: {e}")

    with POOL.connection() as con, con.cursor() as cur:
        row = _fetch_proveedor_by_cuit(cur, cuit_norm)

        if not row:
            # Crear proveedor
            cur.execute("""
              INSERT INTO public.proveedor (cuit, razon_social, email_contacto)
              VALUES (%s, %s, %s)
              RETURNING id
            """, (cuit_norm, body.razon_social, body.email_contacto))
            prov_id = cur.fetchone()[0]
        else:
            prov_id = row[0]
            # Actualizar razón social / email si vienen
            cur.execute("""
              UPDATE public.proveedor
              SET razon_social = COALESCE(%s, razon_social),
                  email_contacto = COALESCE(%s, email_contacto),
                  updated_at = now()
              WHERE id = %s
            """, (body.razon_social, body.email_contacto, prov_id))

        # Manejo de teléfono si vino
        if tel:
            cur.execute("SELECT id FROM public.proveedor WHERE telefono = %s", (tel,))
            holder = cur.fetchone()
            if holder and holder[0] != prov_id:
                if not body.transfer_if_in_use:
                    raise HTTPException(409, "Ese teléfono ya está asignado a otro proveedor")
                # transferir
                cur.execute("UPDATE public.proveedor SET telefono = NULL WHERE id = %s", (holder[0],))
            cur.execute("UPDATE public.proveedor SET telefono = %s, updated_at = now() WHERE id = %s", (tel, prov_id))

        # Devolver fila final
        cur.execute("""
          SELECT id, cuit, razon_social, email_contacto, telefono, created_at, updated_at
          FROM public.proveedor
          WHERE id = %s
        """, (prov_id,))
        out = cur.fetchone(); con.commit()

    return ProviderOut(
        id=out[0], cuit=out[1], razon_social=out[2], email_contacto=out[3],
        telefono=out[4], created_at=str(out[5]) if out[5] else None, updated_at=str(out[6]) if out[6] else None
    )

@router.post("/upsert-telefono", response_model=ProviderOut)
def upsert_telefono(body: UpsertPhoneIn):
    """
    Setea o actualiza el teléfono (E.164) del proveedor indicado por CUIT.
    - Si otro proveedor ya tiene ese teléfono:
        * transfer_if_in_use=false -> 409 Conflict
        * transfer_if_in_use=true  -> mueve el teléfono a este proveedor (deja NULL en el anterior)
    """
    cuit_norm = _norm_cuit(body.cuit)
    if not CUIT_RE.fullmatch(cuit_norm or ""):
        raise HTTPException(400, "CUIT inválido")
    try:
        tel = _to_e164(body.telefono)
    except Exception as e:
        raise HTTPException(400, f"Teléfono inválido: {e}")

    with POOL.connection() as con, con.cursor() as cur:
        dst = _fetch_proveedor_by_cuit(cur, cuit_norm)
        if not dst:
            raise HTTPException(404, "Proveedor no encontrado para ese CUIT")
        dst_id = dst[0]

        cur.execute("SELECT id FROM public.proveedor WHERE telefono = %s", (tel,))
        holder = cur.fetchone()
        if holder and holder[0] != dst_id:
            if not body.transfer_if_in_use:
                raise HTTPException(409, "Ese teléfono ya está asignado a otro proveedor")
            cur.execute("UPDATE public.proveedor SET telefono = NULL WHERE id = %s", (holder[0],))

        cur.execute("""
          UPDATE public.proveedor
          SET telefono = %s, updated_at = now()
          WHERE id = %s
          RETURNING id, cuit, razon_social, email_contacto, telefono, created_at, updated_at
        """, (tel, dst_id))
        row = cur.fetchone(); con.commit()

    return ProviderOut(
        id=row[0], cuit=row[1], razon_social=row[2], email_contacto=row[3],
        telefono=row[4], created_at=str(row[5]) if row[5] else None, updated_at=str(row[6]) if row[6] else None
    )

@router.post("/agregar-a-pedido")
def agregar_a_pedido(body: AddProveedorToPedidoIn):
    """
    Vincula un proveedor (por CUIT) a un pedido (pedido_proveedor).
    - Crea el proveedor si no existe (con razon_social de body o 'Proveedor <CUIT>').
    - Si viene 'telefono', lo setea/actualiza (conflicto -> 409).
    - Si set_adjudicado=true y rol='adjudicatario', actualiza pedido.adjudicado_a = proveedor_id.
    """
    cuit_norm = _norm_cuit(body.cuit)
    if not CUIT_RE.fullmatch(cuit_norm or ""):
        raise HTTPException(400, "CUIT inválido")

    tel: Optional[str] = None
    if body.telefono:
        try:
            tel = _to_e164(body.telefono)
        except Exception as e:
            raise HTTPException(400, f"Teléfono inválido: {e}")

    with POOL.connection() as con, con.cursor() as cur:
        # confirmar que el pedido existe
        cur.execute("SELECT 1 FROM public.pedido WHERE id = %s", (body.pedido_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Pedido no encontrado")

        # asegurar proveedor
        prov_id = _ensure_proveedor(cur, cuit_norm, body.razon_social, body.email_contacto)

        # si viene teléfono, setear (si ya lo tiene otro proveedor -> 409)
        if tel:
            cur.execute("SELECT id FROM public.proveedor WHERE telefono = %s AND id <> %s", (tel, prov_id))
            holder = cur.fetchone()
            if holder:
                raise HTTPException(409, "Ese teléfono ya está asignado a otro proveedor")
            cur.execute("UPDATE public.proveedor SET telefono = %s, updated_at = now() WHERE id = %s", (tel, prov_id))

        # vincular en pedido_proveedor
        cur.execute("""
          INSERT INTO public.pedido_proveedor (pedido_id, proveedor_id, rol)
          VALUES (%s, %s, %s)
          ON CONFLICT DO NOTHING
        """, (body.pedido_id, prov_id, body.rol))

        # opcional adjudicado
        if body.set_adjudicado and body.rol == "adjudicatario":
            cur.execute("UPDATE public.pedido SET adjudicado_a = %s WHERE id = %s", (prov_id, body.pedido_id))

        con.commit()

        # devolver resumen
        cur.execute("""
          SELECT p.id, COALESCE(p.numero,'(s/n)'), p.estado, pr.id, pr.cuit, pr.razon_social, pr.telefono
          FROM public.pedido p
          JOIN public.proveedor pr ON pr.id = %s
          WHERE p.id = %s
        """, (prov_id, body.pedido_id))
        row = cur.fetchone()

    return {
        "ok": True,
        "pedido": {"id": row[0], "numero": row[1], "estado": row[2]},
        "proveedor": {"id": row[3], "cuit": row[4], "razon_social": row[5], "telefono": row[6]},
        "rol": body.rol,
        "adjudicado_set": bool(body.set_adjudicado and body.rol == "adjudicatario"),
    }
