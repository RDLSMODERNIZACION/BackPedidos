# app/routes/wsp.py
# WhatsApp Proveedores: Magic Link + Webhook + Comandos básicos
# Requisitos: fastapi, httpx, PyJWT, psycopg[binary,pool]
#   pip install fastapi "psycopg[binary,pool]" httpx PyJWT

import os, time, jwt, httpx
from uuid import uuid4
from urllib.parse import quote_plus
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

# ========== Config WhatsApp Cloud API ==========
GRAPH = os.getenv("META_GRAPH_BASE", "https://graph.facebook.com")
GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v21.0")
WSP_TOKEN = os.getenv("WSP_TOKEN")                          # token sistema (WABA)
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID_PROVEEDORES")  # emisor p/ proveedores (sin '+')
WABA_DISPLAY_NUMBER = os.getenv("WABA_DISPLAY_NUMBER", "54911XXXXXXXX")  # para wa.me

# ========== Verificación Webhook ==========
VERIFY_TOKEN = os.getenv("WSP_VERIFY_TOKEN", "dirac-wsp-verify-20251013")

# ========== Magic Link (JWT) ==========
JWT_SECRET = os.getenv("WSP_LINK_SECRET", "QxZCk8q9Yp3w7L1nT4v6Rg2sF8m0Jd5Kc9Ub3Xe7Ha1Nr4Vt6Wz2Py8Ql0So5Tu")
JWT_ISS = "dirac-wsp"
JWT_AUD = "wsp_link"

# ========== DB (psycopg3 + pool) ==========
from psycopg_pool import ConnectionPool

DB_URL = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
if not DB_URL:
    raise RuntimeError("Falta SUPABASE_DB_URL o DATABASE_URL")

POOL = ConnectionPool(DB_URL)

def db_exec(sql: str, params: tuple = ()) -> None:
    with POOL.connection() as con, con.cursor() as cur:
        cur.execute(sql, params)
        con.commit()

def db_fetchone(sql: str, params: tuple = ()):
    with POOL.connection() as con, con.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()

def db_fetchall(sql: str, params: tuple = ()):
    with POOL.connection() as con, con.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()

router = APIRouter(prefix="/wsp", tags=["whatsapp"])

# ========== Utils ==========
def _msisdn_plus(msisdn_from_webhook: str) -> str:
    """Meta envía 'from' sin '+'. Normalizamos a E.164 con '+'."""
    return msisdn_from_webhook if msisdn_from_webhook.startswith('+') else f'+{msisdn_from_webhook}'

def send_text(to_msisdn_no_plus: str, text: str):
    """Responder dentro de la ventana (gratis)."""
    if not (WSP_TOKEN and PHONE_NUMBER_ID):
        return
    url = f"{GRAPH}/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WSP_TOKEN}"}
    body = {
        "messaging_product": "whatsapp",
        "to": to_msisdn_no_plus,  # sin '+'
        "type": "text",
        "text": {"preview_url": False, "body": text[:4000]}
    }
    with httpx.Client(timeout=15) as c:
        c.post(url, json=body, headers=headers)

# ========== Magic link: crear ==========
class MagicLinkReq(BaseModel):
    provider_id: int  # proveedor.id a vincular

@router.post("/magiclink")
def create_magic_link(body: MagicLinkReq):
    now = int(time.time())
    exp = now + 10 * 60  # 10 min
    jti = str(uuid4())
    payload = {"iss": JWT_ISS, "aud": JWT_AUD, "iat": now, "exp": exp, "jti": jti, "provider_id": body.provider_id}
    token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")

    # Guardar jti (antireplay)
    db_exec(
        "INSERT INTO public.wsp_link_tokens (jti, provider_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (jti, body.provider_id),
    )

    # Texto: "VINCULAR <token>" → deep link a wa.me
    text = f"VINCULAR {token}"
    link = f"https://wa.me/{WABA_DISPLAY_NUMBER}?text={quote_plus(text)}"
    return {"link": link, "expires_in_sec": 600}

# ========== Webhook GET (verify) ==========
@router.get("/webhook")
def verify_webhook(request: Request):
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == VERIFY_TOKEN:
        return int(params.get("hub.challenge", "0"))
    raise HTTPException(403, "Verify token inválido")

# ========== Webhook POST (mensajes) ==========
@router.post("/webhook")
def receive_webhook(payload: dict):
    try:
        changes = payload["entry"][0]["changes"][0]["value"]
    except Exception:
        return {"ok": True}  # ignorar payloads no estándar

    if "messages" not in changes:
        return {"ok": True}  # delivery/read/etc.

    msg = changes["messages"][0]
    from_no_plus = msg.get("from") or ""     # ej: 5492993251398
    msisdn = _msisdn_plus(from_no_plus)      # +5492993251398
    body = (msg.get("text", {}) or {}).get("body", "") or ""
    UP = body.strip().upper()

    # Touch actividad (métrica / ventana 24h)
    db_exec("UPDATE public.provider_contacts SET last_seen_at = now() WHERE msisdn = %s", (msisdn,))

    # ---- VINCULAR <token> ----
    if UP.startswith("VINCULAR "):
        token = body.split(" ", 1)[1].strip()
        try:
            data = jwt.decode(token, JWT_SECRET, algorithms=["HS256"], audience=JWT_AUD, issuer=JWT_ISS)
        except Exception:
            send_text(from_no_plus, "Token inválido o vencido. Volvé a generar el enlace desde el portal.")
            return {"ok": False, "reason": "bad_token"}

        jti = data["jti"]
        provider_id = int(data["provider_id"])

        row = db_fetchone("SELECT consumed_at FROM public.wsp_link_tokens WHERE jti=%s", (jti,))
        if row and row[0] is not None:
            send_text(from_no_plus, "Ese enlace ya fue usado. Generá uno nuevo.")
            return {"ok": False, "reason": "replay"}

        # Vincular/verificar msisdn
        db_exec(
            """
            INSERT INTO public.provider_contacts (proveedor_id, msisdn, verified, last_seen_at)
            VALUES (%s, %s, TRUE, now())
            ON CONFLICT (proveedor_id, msisdn)
            DO UPDATE SET verified=TRUE, last_seen_at=now()
            """,
            (provider_id, msisdn),
        )
        db_exec("UPDATE public.wsp_link_tokens SET consumed_at = now() WHERE jti=%s", (jti,))

        send_text(from_no_plus, "Listo ✅ Tu WhatsApp quedó vinculado. Comandos: MIS PEDIDOS, CONSULTAR PEDIDO <ID>, DESVINCULAR")
        return {"ok": True}

    # ---- MIS PEDIDOS [N] ----
    if UP.startswith("MIS PEDIDOS"):
        parts = UP.split()
        limit = 5
        if len(parts) == 3 and parts[2].isdigit():
            limit = max(1, min(10, int(parts[2])))

        rows = db_fetchall(
            """
            SELECT p.id, COALESCE(p.numero,'(s/n)'), p.estado, p.updated_at
            FROM public.pedido p
            JOIN public.provider_contacts pc
              ON pc.proveedor_id = p.adjudicado_a AND pc.verified = TRUE
            WHERE pc.msisdn = %s
            ORDER BY p.updated_at DESC
            LIMIT %s
            """,
            (msisdn, limit),
        )

        if not rows:
            send_text(from_no_plus, "No tenés pedidos adjudicados.")
        else:
            lines = [f"{r[1]} (ID {r[0]}) · {r[2]} · {r[3]:%Y-%m-%d}" for r in rows]
            send_text(from_no_plus, "Tus pedidos:\n" + "\n".join(lines))
        return {"ok": True}

    # ---- CONSULTAR PEDIDO <ID> ----
    if UP.startswith("CONSULTAR PEDIDO"):
        parts = UP.split()
        if len(parts) >= 3 and parts[-1].isdigit():
            pedido_id = int(parts[-1])
            row = db_fetchone(
                """
                SELECT p.id, COALESCE(p.numero,'(s/n)'), p.estado, p.updated_at
                FROM public.pedido p
                JOIN public.provider_contacts pc
                  ON pc.proveedor_id = p.adjudicado_a AND pc.verified = TRUE
                WHERE pc.msisdn = %s AND p.id = %s
                """,
                (msisdn, pedido_id),
            )
            if row:
                pid, numero, estado, upd = row
                send_text(from_no_plus, f"Pedido {numero} (ID {pid})\nEstado: {estado}\nActualizado: {upd:%Y-%m-%d %H:%M}")
            else:
                send_text(from_no_plus, "No encuentro ese pedido.")
        else:
            send_text(from_no_plus, "Formato: CONSULTAR PEDIDO <ID> (ej: CONSULTAR PEDIDO 49)")
        return {"ok": True}

    # ---- DESVINCULAR ----
    if UP.startswith("DESVINCULAR"):
        db_exec("DELETE FROM public.provider_contacts WHERE msisdn=%s", (msisdn,))
        send_text(from_no_plus, "Listo. Tu número fue desvinculado.")
        return {"ok": True}

    # Fallback ayuda
    send_text(from_no_plus, "Comandos:\n• MIS PEDIDOS [N]\n• CONSULTAR PEDIDO <ID>\n• DESVINCULAR")
    return {"ok": True}
