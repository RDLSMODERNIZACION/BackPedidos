# app/routes/wsp.py
# WhatsApp Proveedores — flujo base por teléfono en proveedor (1 teléfono por proveedor)
# Requisitos: fastapi, httpx, psycopg[binary,pool]
#   pip install fastapi httpx "psycopg[binary,pool]"

import os, httpx, re
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from psycopg_pool import ConnectionPool

router = APIRouter(prefix="/wsp", tags=["whatsapp"])

# ========== Config WhatsApp Cloud API ==========
GRAPH = os.getenv("META_GRAPH_BASE", "https://graph.facebook.com")
GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v22.0")
WSP_TOKEN = os.getenv("WSP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID_PROVEEDORES")
WABA_DISPLAY_NUMBER = os.getenv("WABA_DISPLAY_NUMBER", "15551630027")

# ========== Webhook verify (GET) ==========
VERIFY_TOKEN = os.getenv("WSP_VERIFY_TOKEN", "dirac-wsp-verify-20251013")

@router.get("/webhook")
def verify_webhook(request: Request):
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == VERIFY_TOKEN:
        return Response(content=p.get("hub.challenge", "0"), media_type="text/plain")
    raise HTTPException(403, "Verify token inválido")

# ========== DB pool ==========
DB_URL = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
if not DB_URL:
    raise RuntimeError("Falta SUPABASE_DB_URL o DATABASE_URL")
POOL = ConnectionPool(DB_URL)

# ========== Helpers ==========
def _digits_only(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())

def _msisdn_plus(from_meta: str) -> str:
    if not from_meta:
        return ""
    return from_meta if from_meta.startswith('+') else f'+{from_meta}'

def _require_env():
    missing = []
    if not WSP_TOKEN: missing.append("WSP_TOKEN")
    if not PHONE_NUMBER_ID: missing.append("PHONE_NUMBER_ID_PROVEEDORES")
    if missing:
        raise HTTPException(500, f"Faltan variables de entorno: {', '.join(missing)}")

def _post_once(payload: dict):
    url = f"{GRAPH}/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WSP_TOKEN}"}
    try:
        with httpx.Client(timeout=15) as c:
            r = c.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Error HTTP al llamar a Meta: {e}") from e
    if r.status_code < 300:
        try:
            return True, r.json(), r.status_code
        except Exception:
            return True, r.text, r.status_code
    return False, r.text, r.status_code

def _candidate_to_variants(to_digits: str):
    # Maneja AR 54/549 automáticamente (evita 131030 cuando la allow-list no coincide)
    if to_digits.startswith("549"):
        return [to_digits, "54" + to_digits[3:]]
    if to_digits.startswith("54"):
        return [to_digits, "549" + to_digits[2:]]
    return [to_digits]

def _send_with_fallback(build_payload_fn, to_msisdn_no_plus: str):
    _require_env()
    digits = _digits_only(to_msisdn_no_plus)
    variants = _candidate_to_variants(digits)
    last_err_text = None; last_status = None
    for cand in variants:
        ok, data, status = _post_once(build_payload_fn(cand))
        if ok: return data
        if "131030" in str(data) or "not in allowed list" in str(data).lower():
            last_err_text, last_status = data, status
            continue
        raise HTTPException(status, str(data))
    raise HTTPException(last_status or 400, str(last_err_text) if last_err_text else "send error")

def send_text(to_msisdn_no_plus: str, text: str):
    def _builder(to_digits: str):
        return {
            "messaging_product": "whatsapp",
            "to": to_digits,
            "type": "text",
            "text": {"preview_url": False, "body": text[:4000]},
        }
    return _send_with_fallback(_builder, to_msisdn_no_plus)

def _provider_id_for_msisdn(msisdn_plus: str) -> int | None:
    if not msisdn_plus:
        return None
    with POOL.connection() as con, con.cursor() as cur:
        cur.execute("SELECT id FROM public.proveedor WHERE telefono = %s LIMIT 1", (msisdn_plus,))
        row = cur.fetchone()
        return row[0] if row else None

# ========== Health ==========
@router.get("/health")
def health():
    return {"ok": True, "graph": GRAPH, "version": GRAPH_VERSION, "has_token": bool(WSP_TOKEN), "has_phone_id": bool(PHONE_NUMBER_ID)}

# ========== Endpoints DE PRUEBA ==========
class SendTextReq(BaseModel):
    to: str
    text: str = "Prueba Dirac ✅ — hola!"

@router.post("/test/text")
def send_text_minimal(body: SendTextReq):
    return send_text(body.to, body.text)

class SendTemplateReq(BaseModel):
    to: str
    template_name: str = "hello_world"
    language_code: str = "en_US"

@router.post("/test/template")
def send_template_minimal(body: SendTemplateReq):
    def _builder(to_digits: str):
        return {
            "messaging_product": "whatsapp",
            "to": to_digits,
            "type": "template",
            "template": {"name": body.template_name, "language": {"code": body.language_code}},
        }
    return _send_with_fallback(_builder, body.to)

# ========== Webhook ==========
@router.post("/webhook")
def receive_webhook(payload: dict):
    # Estructura básica de Webhook de Meta
    try:
        changes = payload["entry"][0]["changes"][0]["value"]
    except Exception:
        return {"ok": True}
    if "messages" not in changes:
        return {"ok": True}

    msg = changes["messages"][0]
    from_no_plus = msg.get("from") or ""
    msisdn = _msisdn_plus(from_no_plus)  # +542993251398 etc.
    body = (msg.get("text", {}) or {}).get("body", "") or ""
    UP = body.strip().upper()

    # Resolver proveedor por teléfono
    prov_id = _provider_id_for_msisdn(msisdn)

    # ---- DESVINCULAR (borra el teléfono de proveedor) ----
    if UP.startswith("DESVINCULAR"):
        with POOL.connection() as con, con.cursor() as cur:
            cur.execute("UPDATE public.proveedor SET telefono = NULL WHERE telefono = %s", (msisdn,))
            con.commit()
        send_text(from_no_plus, "Listo. Este número fue desvinculado.")
        return {"ok": True}

    # ---- MIS PEDIDOS [N] ----
    if UP.startswith("MIS PEDIDOS"):
        if prov_id is None:
            send_text(from_no_plus, "No encuentro un proveedor para este número. Pedí a Compras que te registren el teléfono.")
            return {"ok": False, "reason": "no_provider"}

        parts = UP.split()
        limit = 5
        if len(parts) == 3 and parts[2].isdigit():
            limit = max(1, min(10, int(parts[2])))

        with POOL.connection() as con, con.cursor() as cur:
            cur.execute("""
              SELECT p.id, COALESCE(p.numero,'(s/n)'), p.estado, p.updated_at
              FROM public.pedido_proveedor pp
              JOIN public.pedido p ON p.id = pp.pedido_id
              WHERE pp.proveedor_id = %s
              ORDER BY p.updated_at DESC
              LIMIT %s
            """, (prov_id, limit))
            rows = cur.fetchall()

        if not rows:
            send_text(from_no_plus, "No tenés pedidos vinculados.")
        else:
            lines = [f"{r[1]} (ID {r[0]}) · {r[2]} · {r[3]:%Y-%m-%d}" for r in rows]
            send_text(from_no_plus, "Tus pedidos:\n" + "\n".join(lines))
        return {"ok": True}

    # ---- CONSULTAR PEDIDO <ID|NUMERO> ----
    if UP.startswith("CONSULTAR PEDIDO"):
        if prov_id is None:
            send_text(from_no_plus, "No encuentro un proveedor para este número. Pedí a Compras que te registren el teléfono.")
            return {"ok": False, "reason": "no_provider"}

        tail = body.split("CONSULTAR PEDIDO", 1)[1].strip()
        is_id = re.fullmatch(r"\d+", tail) is not None

        with POOL.connection() as con, con.cursor() as cur:
            if is_id:
                cur.execute("""
                  SELECT p.id, COALESCE(p.numero,'(s/n)'), p.estado, p.updated_at
                  FROM public.pedido_proveedor pp
                  JOIN public.pedido p ON p.id = pp.pedido_id
                  WHERE pp.proveedor_id = %s AND p.id = %s
                """, (prov_id, int(tail)))
            else:
                cur.execute("""
                  SELECT p.id, COALESCE(p.numero,'(s/n)'), p.estado, p.updated_at
                  FROM public.pedido_proveedor pp
                  JOIN public.pedido p ON p.id = pp.pedido_id
                  WHERE pp.proveedor_id = %s AND p.numero = %s
                """, (prov_id, tail))
            row = cur.fetchone()

        if row:
            pid, num, estado, upd = row
            send_text(from_no_plus, f"Pedido {num} (ID {pid})\nEstado: {estado}\nActualizado: {upd:%Y-%m-%d %H:%M}")
        else:
            send_text(from_no_plus, "No encuentro ese pedido o no está vinculado a tu CUIT.")
        return {"ok": True}

    # Fallback ayuda
    send_text(from_no_plus, "Comandos:\n• MIS PEDIDOS [N]\n• CONSULTAR PEDIDO <ID|NUMERO>\n• DESVINCULAR")
    return {"ok": True}
