# app/routes/wsp.py
# WhatsApp Proveedores — Minimal + DB (test endpoints + magic link + webhook)
# Requisitos: fastapi, httpx, PyJWT, psycopg[binary,pool]
#   pip install fastapi httpx "psycopg[binary,pool]" PyJWT

import os, time, jwt, httpx, re
from uuid import uuid4
from urllib.parse import quote_plus
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from psycopg_pool import ConnectionPool

router = APIRouter(prefix="/wsp", tags=["whatsapp"])

# ========== Config WhatsApp Cloud API ==========
GRAPH = os.getenv("META_GRAPH_BASE", "https://graph.facebook.com")
GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v22.0")     # podés usar v21.0 si querés
WSP_TOKEN = os.getenv("WSP_TOKEN")                            # Access token (Bearer)
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID_PROVEEDORES")    # Phone Number ID (test/prod)
WABA_DISPLAY_NUMBER = os.getenv("WABA_DISPLAY_NUMBER", "15551630027")  # para wa.me (opcional)

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
def _digits_only(e164: str) -> str:
    """E.164 sin '+' (solo dígitos) para la Cloud API."""
    return "".join(ch for ch in e164 if ch.isdigit())

def _msisdn_plus(from_meta: str) -> str:
    """Meta envía 'from' SIN '+'. Normalizamos a E.164 con '+'."""
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
    """Envía 1 payload. Devuelve (ok, json/text, status_code)."""
    url = f"{GRAPH}/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WSP_TOKEN}"}
    with httpx.Client(timeout=15) as c:
        r = c.post(url, json=payload, headers=headers)
        if r.status_code < 300:
            try:
                return True, r.json(), r.status_code
            except Exception:
                return True, r.text, r.status_code
        return False, r.text, r.status_code

def _candidate_to_variants(to_digits: str):
    """
    Genera variantes de destino para salvar whitelists raras (54/549/15).
    Orden de intento:
      - Si arranca con 549: [549..., 54...]
      - Si arranca con 54 (y no 549): [54..., 549...]
      - Si otra cosa: [to_digits]
    """
    variants = [to_digits]
    if to_digits.startswith("549"):
        alt = "54" + to_digits[3:]  # dropear el 9
        variants = [to_digits, alt]
    elif to_digits.startswith("54") and not to_digits.startswith("549"):
        alt = "549" + to_digits[2:]  # insertar el 9
        variants = [to_digits, alt]
    return variants

def _send_with_fallback(build_payload_fn, to_msisdn_no_plus: str):
    """
    Intenta enviar a 549... y, si falla por (#131030) not-in-allowed-list,
    reintenta con 54... (o viceversa). No cambia la DB.
    """
    _require_env()
    base_digits = _digits_only(to_msisdn_no_plus)
    variants = _candidate_to_variants(base_digits)

    last_err_text = None
    last_status = None

    for cand in variants:
        payload = build_payload_fn(cand)
        ok, data, status = _post_once(payload)
        if ok:
            return data
        # Si es 131030 probamos siguiente candidato
        if "131030" in str(data) or "not in allowed list" in str(data).lower():
            last_err_text, last_status = data, status
            continue
        # Otro error: devolvemos tal cual
        raise HTTPException(status, str(data))

    # Si agotamos variantes, devolvemos el último error (probablemente 131030)
    raise HTTPException(last_status or 400, str(last_err_text) if last_err_text else "send error")

def send_text(to_msisdn_no_plus: str, text: str):
    """Enviar texto con fallback 549/54."""
    def _builder(to_digits: str):
        return {
            "messaging_product": "whatsapp",
            "to": to_digits,
            "type": "text",
            "text": {"preview_url": False, "body": text[:4000]},
        }
    return _send_with_fallback(_builder, to_msisdn_no_plus)

# ========== Health check ==========
@router.get("/health")
def health():
    return {
        "ok": True,
        "graph": GRAPH,
        "version": GRAPH_VERSION,
        "has_token": bool(WSP_TOKEN),
        "has_phone_id": bool(PHONE_NUMBER_ID),
    }

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
    """Envía plantilla con fallback 549/54."""
    def _builder(to_digits: str):
        return {
            "messaging_product": "whatsapp",
            "to": to_digits,
            "type": "template",
            "template": {"name": body.template_name, "language": {"code": body.language_code}},
        }
    return _send_with_fallback(_builder, body.to)

# ========== Magic link (JWT) ==========
JWT_SECRET = os.getenv("WSP_LINK_SECRET", "QxZCk8q9Yp3w7L1nT4v6Rg2sF8m0Jd5Kc9Ub3Xe7Ha1Nr4Vt6Wz2Py8Ql0So5Tu")
JWT_ISS = "dirac-wsp"
JWT_AUD = "wsp_link"

class MagicLinkReq(BaseModel):
    provider_id: int

@router.post("/magiclink")
def create_magic_link(body: MagicLinkReq):
    now = int(time.time())
    exp = now + 10 * 60
    jti = str(uuid4())
    payload = {"iss": JWT_ISS, "aud": JWT_AUD, "iat": now, "exp": exp, "jti": jti, "provider_id": body.provider_id}
    token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
    with POOL.connection() as con, con.cursor() as cur:
        cur.execute("INSERT INTO public.wsp_link_tokens (jti, provider_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (jti, body.provider_id))
        con.commit()
    text = f"VINCULAR {token}"
    link = f"https://wa.me/{WABA_DISPLAY_NUMBER}?text={quote_plus(text)}"
    return {"link": link, "expires_in_sec": 600}

# ========== Webhook (POST) — identidad + comandos ==========
@router.post("/webhook")
def receive_webhook(payload: dict):
    try:
        changes = payload["entry"][0]["changes"][0]["value"]
    except Exception:
        return {"ok": True}

    if "messages" not in changes:
        return {"ok": True}

    msg = changes["messages"][0]
    from_no_plus = msg.get("from") or ""     # ej: 549299...
    msisdn = _msisdn_plus(from_no_plus)      # +549299...
    body = (msg.get("text", {}) or {}).get("body", "") or ""
    UP = body.strip().upper()

    # Touch actividad
    with POOL.connection() as con, con.cursor() as cur:
        cur.execute("UPDATE public.provider_contacts SET last_seen_at = now() WHERE msisdn = %s", (msisdn,))
        con.commit()

    # ---- VINCULAR <token> ----
    if UP.startswith("VINCULAR "):
        token = body.split(" ", 1)[1].strip()
        try:
            data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"], audience=JWT_AUD, issuer=JWT_ISS)
        except Exception:
            send_text(from_no_plus, "Token inválido o vencido. Volvé a generar el enlace desde el portal.")
            return {"ok": False, "reason": "bad_token"}
        jti = data["jti"]; provider_id = int(data["provider_id"])

        with POOL.connection() as con, con.cursor() as cur:
            # antireplay
            cur.execute("SELECT consumed_at FROM public.wsp_link_tokens WHERE jti=%s", (jti,))
            row = cur.fetchone()
            if row and row[0] is not None:
                send_text(from_no_plus, "Ese enlace ya fue usado. Generá uno nuevo.")
                return {"ok": False, "reason": "replay"}

            # upsert contacto
            cur.execute("""
              INSERT INTO public.provider_contacts (proveedor_id, msisdn, verified, last_seen_at)
              VALUES (%s, %s, TRUE, now())
              ON CONFLICT (proveedor_id, msisdn)
              DO UPDATE SET verified=TRUE, last_seen_at=now()
            """, (provider_id, msisdn))
            cur.execute("UPDATE public.wsp_link_tokens SET consumed_at = now() WHERE jti=%s", (jti,))
            con.commit()

        send_text(from_no_plus, "Listo ✅ Tu WhatsApp quedó vinculado. Comandos: MIS PEDIDOS, CONSULTAR PEDIDO <ID|NUMERO>, DESVINCULAR")
        return {"ok": True}

    # ---- MIS PEDIDOS [N] ----
    if UP.startswith("MIS PEDIDOS"):
        parts = UP.split()
        limit = 5
        if len(parts) == 3 and parts[2].isdigit():
            limit = max(1, min(10, int(parts[2])))

        with POOL.connection() as con, con.cursor() as cur:
            cur.execute("""
              SELECT p.id, COALESCE(p.numero,'(s/n)'), p.estado, p.updated_at
              FROM public.pedido_proveedor pp
              JOIN public.provider_contacts pc ON pc.proveedor_id = pp.proveedor_id AND pc.verified = TRUE
              JOIN public.pedido p ON p.id = pp.pedido_id
              WHERE pc.msisdn = %s
              ORDER BY p.updated_at DESC
              LIMIT %s
            """, (msisdn, limit))
            rows = cur.fetchall()

        if not rows:
            send_text(from_no_plus, "No tenés pedidos vinculados.")
        else:
            lines = [f"{r[1]} (ID {r[0]}) · {r[2]} · {r[3]:%Y-%m-%d}" for r in rows]
            send_text(from_no_plus, "Tus pedidos:\n" + "\n".join(lines))
        return {"ok": True}

    # ---- CONSULTAR PEDIDO <ID|NUMERO> ----
    if UP.startswith("CONSULTAR PEDIDO"):
        pedido_id = None
        numero = None
        tail = body.split("CONSULTAR PEDIDO", 1)[1].strip()
        if re.fullmatch(r"\d+", tail):
            pedido_id = int(tail)
        else:
            numero = tail

        with POOL.connection() as con, con.cursor() as cur:
            if pedido_id is not None:
                cur.execute("""
                  SELECT p.id, COALESCE(p.numero,'(s/n)'), p.estado, p.updated_at
                  FROM public.pedido_proveedor pp
                  JOIN public.provider_contacts pc ON pc.proveedor_id = pp.proveedor_id AND pc.verified = TRUE
                  JOIN public.pedido p ON p.id = pp.pedido_id
                  WHERE pc.msisdn = %s AND p.id = %s
                """, (msisdn, pedido_id))
            else:
                cur.execute("""
                  SELECT p.id, COALESCE(p.numero,'(s/n)'), p.estado, p.updated_at
                  FROM public.pedido_proveedor pp
                  JOIN public.provider_contacts pc ON pc.proveedor_id = pp.proveedor_id AND pc.verified = TRUE
                  JOIN public.pedido p ON p.id = pp.pedido_id
                  WHERE pc.msisdn = %s AND p.numero = %s
                """, (msisdn, numero))
            row = cur.fetchone()

        if row:
            pid, num, estado, upd = row
            send_text(from_no_plus, f"Pedido {num} (ID {pid})\nEstado: {estado}\nActualizado: {upd:%Y-%m-%d %H:%M}")
        else:
            send_text(from_no_plus, "No encuentro ese pedido o no estás vinculado.")
        return {"ok": True}

    # ---- DESVINCULAR ----
    if UP.startswith("DESVINCULAR"):
        with POOL.connection() as con, con.cursor() as cur:
            cur.execute("DELETE FROM public.provider_contacts WHERE msisdn=%s", (msisdn,))
            con.commit()
        send_text(from_no_plus, "Listo. Tu número fue desvinculado.")
        return {"ok": True}

    # Fallback ayuda
    send_text(from_no_plus, "Comandos:\n• MIS PEDIDOS [N]\n• CONSULTAR PEDIDO <ID|NUMERO>\n• DESVINCULAR")
    return {"ok": True}
