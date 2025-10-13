# app/routes/wsp.py
# WhatsApp Cloud API - Versión mínima de prueba (sin DB)
# Requisitos: fastapi, httpx
#   pip install fastapi httpx

import os
import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

router = APIRouter(prefix="/wsp", tags=["whatsapp"])

# ========== Config WhatsApp Cloud API ==========
GRAPH = os.getenv("META_GRAPH_BASE", "https://graph.facebook.com")
GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v22.0")  # alineado con el cURL que te funciona
WSP_TOKEN = os.getenv("WSP_TOKEN")                        # Access token (Bearer)
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID_PROVEEDORES")  # Phone Number ID del emisor (test number)

# ========== Webhook verify (GET) ==========
VERIFY_TOKEN = os.getenv("WSP_VERIFY_TOKEN", "dirac-wsp-verify-20251013")

@router.get("/webhook")
def verify_webhook(request: Request):
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == VERIFY_TOKEN:
        return Response(content=p.get("hub.challenge", "0"), media_type="text/plain")
    raise HTTPException(403, "Verify token inválido")

# ========== Helpers ==========
def _digits_only(e164: str) -> str:
    """E.164 sin '+' (solo dígitos) para la API."""
    return "".join(ch for ch in e164 if ch.isdigit())

def _require_env():
    missing = []
    if not WSP_TOKEN: missing.append("WSP_TOKEN")
    if not PHONE_NUMBER_ID: missing.append("PHONE_NUMBER_ID_PROVEEDORES")
    if missing:
        raise HTTPException(500, f"Faltan variables de entorno: {', '.join(missing)}")

def _post_to_whatsapp(payload: dict):
    url = f"{GRAPH}/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WSP_TOKEN}"}
    with httpx.Client(timeout=15) as c:
        r = c.post(url, json=payload, headers=headers)
        if r.status_code >= 300:
            # Devolver el error tal cual para entender qué pasa (131030, 190, etc.)
            raise HTTPException(r.status_code, r.text)
        return r.json()

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

# ========== Envío de TEXTO (simple) ==========
class SendTextReq(BaseModel):
    to: str                  # destino en E.164 (con o sin '+', ej +549299...)
    text: str = "Prueba Dirac ✅ — hola!"

@router.post("/test/text")
def send_text_minimal(body: SendTextReq):
    """
    Envía un texto simple.
    - Requiere ventana de 24 h abierta (o que el usuario te haya escrito primero).
    - Para test number, el 'to' debe estar en la lista de destinatarios de prueba.
    """
    _require_env()
    to_norm = _digits_only(body.to)
    payload = {
        "messaging_product": "whatsapp",
        "to": to_norm,
        "type": "text",
        "text": {"preview_url": False, "body": body.text[:4000]},
    }
    return _post_to_whatsapp(payload)

# ========== Envío de PLANTILLA (hello_world) ==========
class SendTemplateReq(BaseModel):
    to: str                        # destino en E.164
    template_name: str = "hello_world"
    language_code: str = "en_US"   # p.ej. es_AR cuando uses tus plantillas

@router.post("/test/template")
def send_template_minimal(body: SendTemplateReq):
    """
    Envía una plantilla (por defecto 'hello_world').
    - Útil cuando NO hay ventana de 24 h abierta.
    - Para test number, el 'to' debe estar en la lista de destinatarios de prueba.
    """
    _require_env()
    to_norm = _digits_only(body.to)
    payload = {
        "messaging_product": "whatsapp",
        "to": to_norm,
        "type": "template",
        "template": {
            "name": body.template_name,
            "language": {"code": body.language_code},
        },
    }
    return _post_to_whatsapp(payload)
