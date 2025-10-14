# app/routes/wsp.py
# WhatsApp Proveedores — flujo base por teléfono en proveedor (1 teléfono por proveedor)
# Requisitos: fastapi, httpx, psycopg[binary,pool]
#   pip install fastapi httpx "psycopg[binary,pool]"

import os, httpx, re
from typing import Optional, List, Tuple
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
ESTADO_EMOJI = {
    "borrador": "📝",
    "enviado": "📤",
    "en_revision": "🕵️",
    "aprobado": "✅",
    "rechazado": "❌",
    "en_proceso": "🔧",
    "area_pago": "💳",
    "cerrado": "🏁",
}

def _estado_badge(estado: Optional[str]) -> str:
    e = (estado or "").lower()
    return f"{ESTADO_EMOJI.get(e, '📄')} {e.replace('_',' ') or 's/n'}"

def _digits_only(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())

def _msisdn_plus(from_meta: str) -> str:
    if not from_meta:
        return ""
    return from_meta if from_meta.startswith('+') else f'+{from_meta}'

def _variants_plus(msisdn_plus: str) -> List[str]:
    """Genera variantes +54... y +549... (Argentina) preservando orden, sin duplicados."""
    d = _digits_only(msisdn_plus)
    if not d:
        return []
    cand = [f'+{d}']
    if d.startswith('549'):
        cand.append(f'+54{d[3:]}')         # sin el 9
    elif d.startswith('54') and not d.startswith('549'):
        cand.append(f'+549{d[2:]}')        # con el 9
    # dedup preservando orden
    seen = set(); out = []
    for x in cand:
        if x not in seen:
            seen.add(x); out.append(x)
    return out

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
    # WhatsApp no soporta "colores", usamos emojis y estructura clara
    def _builder(to_digits: str):
        return {
            "messaging_product": "whatsapp",
            "to": to_digits,
            "type": "text",
            "text": {"preview_url": False, "body": text[:4000]},
        }
    return _send_with_fallback(_builder, to_msisdn_no_plus)

def _provider_id_for_msisdn(msisdn_plus: str) -> Optional[int]:
    variants = _variants_plus(msisdn_plus)
    if not variants:
        return None
    with POOL.connection() as con, con.cursor() as cur:
        cur.execute("SELECT id FROM public.proveedor WHERE telefono = ANY(%s) LIMIT 1", (variants,))
        row = cur.fetchone()
        return row[0] if row else None

def _fetch_mis_pedidos(prov_id: int, limit: int = 5) -> List[Tuple[int, str, str, object]]:
    with POOL.connection() as con, con.cursor() as cur:
        cur.execute("""
          SELECT p.id, COALESCE(p.numero,'(s/n)'), p.estado, p.updated_at
          FROM public.pedido_proveedor pp
          JOIN public.pedido p ON p.id = pp.pedido_id
          WHERE pp.proveedor_id = %s
          ORDER BY p.updated_at DESC
          LIMIT %s
        """, (prov_id, limit))
        return cur.fetchall()

def _fetch_pedido_by_id(prov_id: int, pid: int):
    with POOL.connection() as con, con.cursor() as cur:
        cur.execute("""
          SELECT p.id, COALESCE(p.numero,'(s/n)'), p.estado, p.updated_at
          FROM public.pedido_proveedor pp
          JOIN public.pedido p ON p.id = pp.pedido_id
          WHERE pp.proveedor_id = %s AND p.id = %s
        """, (prov_id, pid))
        return cur.fetchone()

def _fetch_pedido_by_num(prov_id: int, numero: str):
    with POOL.connection() as con, con.cursor() as cur:
        cur.execute("""
          SELECT p.id, COALESCE(p.numero,'(s/n)'), p.estado, p.updated_at
          FROM public.pedido_proveedor pp
          JOIN public.pedido p ON p.id = pp.pedido_id
          WHERE pp.proveedor_id = %s AND p.numero = %s
        """, (prov_id, numero))
        return cur.fetchone()

def _pretty_menu() -> str:
    return (
        "🟩 *DIRAC · Proveedores*\n"
        "Elegí una opción enviando el número o escribiendo el comando:\n"
        "  1) 📋 *Mis pedidos* (opcional: `MIS PEDIDOS 5`)\n"
        "  2) 🔎 *Consultar por Expediente*\n"
        "     Ejemplos: `EXP-2025-0071`, `2 EXP-2025-0071`\n"
        "  3) 🧾 *Consultar por ID*\n"
        "     Ejemplos: `3 83`, `ID 83`, `#83`\n"
        "\n"
        "Tip: después de ver *Mis pedidos*, podés contestar solo el número de la lista (1–9) para ver el detalle. ✅"
    )

def _pretty_list(rows: List[Tuple[int,str,str,object]]) -> str:
    if not rows:
        return "😕 No tenés pedidos vinculados."
    out = ["📋 *Tus pedidos (últimos actualizados)*"]
    for i,(pid,num,estado,upd) in enumerate(rows, start=1):
        out.append(f"{i}) *{num}* · ID {pid} · {_estado_badge(estado)} · {upd:%Y-%m-%d}")
    out.append("\nRespondé el *número* (1–9) para ver el detalle, o mandá `menu` para volver.")
    return "\n".join(out)

def _pretty_detail(row: Tuple[int,str,str,object]) -> str:
    pid, num, estado, upd = row
    return (
        f"🟦 *Detalle de expediente*\n"
        f"*{num}* · ID {pid}\n"
        f"Estado: {_estado_badge(estado)}\n"
        f"Actualizado: {upd:%Y-%m-%d %H:%M}"
    )

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
        return {"ok": True"}

    msg = changes["messages"][0]
    from_no_plus = msg.get("from") or ""
    msisdn = _msisdn_plus(from_no_plus)  # +54... o +549...
    body = (msg.get("text", {}) or {}).get("body", "") or ""
    raw = body.strip()
    UP = raw.upper()

    # Resolver proveedor por teléfono (acepta +54 / +549)
    prov_id = _provider_id_for_msisdn(msisdn)
    if prov_id is None:
        send_text(from_no_plus, "⚠️ No encuentro un proveedor para este número. Pedí a Compras que registren tu teléfono.\n\n" + _pretty_menu())
        return {"ok": False, "reason": "no_provider"}

    # ======= Ruteo de comandos =======
    # 0) MENU / AYUDA
    if UP in ("MENU", "AYUDA", "HELP", "?"):
        send_text(from_no_plus, _pretty_menu())
        return {"ok": True}

    # 1) MIS PEDIDOS [N]  — o número suelto 1..9 (selección del listado reciente)
    # a) "MIS PEDIDOS" o "MIS PEDIDOS N" o "1"
    m_mis = re.fullmatch(r"(MIS\s+PEDIDOS)(\s+(\d+))?$", UP)
    if UP == "1" or m_mis:
        limit = 5
        if m_mis and m_mis.group(3) and m_mis.group(3).isdigit():
            limit = max(1, min(9, int(m_mis.group(3))))
        rows = _fetch_mis_pedidos(prov_id, limit=limit)
        send_text(from_no_plus, _pretty_list(rows))
        return {"ok": True}

    # b) número suelto 1..9 -> interpreta como selección del TOP 9 por updated_at
    if re.fullmatch(r"[1-9]", UP):
        idx = int(UP)
        rows = _fetch_mis_pedidos(prov_id, limit=9)
        if 1 <= idx <= len(rows):
            row = rows[idx-1]
            send_text(from_no_plus, _pretty_detail(row))
        else:
            send_text(from_no_plus, "😕 Número fuera de rango. Escribí `menu` para ver opciones.")
        return {"ok": True}

    # 2) Consultar por EXP (acepta: "2 EXP-2025-0001" ó solo "EXP-2025-0001")
    m_exp2 = re.fullmatch(r"2\s+([A-Z\-0-9/]+)", UP)
    m_exp  = re.fullmatch(r"(EXP[\-\s]?\d{4}[\-\s]?\d+)", UP)  # EXP-2025-0071 / EXP 2025 0071
    numero = None
    if m_exp2:
        numero = m_exp2.group(1).replace(" ", "").upper()
    elif m_exp:
        numero = m_exp.group(1).replace(" ", "").upper()
    if numero:
        # normalizar EXP-YYYY-XXXX
        numero = numero.replace("EXP", "EXP-").replace("--","-")
        numero = re.sub(r"EXP-?(\d{4})-?(\d+)", r"EXP-\1-\2", numero)
        row = _fetch_pedido_by_num(prov_id, numero)
        if row:
            send_text(from_no_plus, _pretty_detail(row))
        else:
            send_text(from_no_plus, f"🔎 No encuentro *{numero}* vinculado a tu CUIT.\nEscribí `menu` para ver opciones.")
        return {"ok": True}

    # 3) Consultar por ID (acepta: "3 83", "ID 83", "#83")
    m_id3 = re.fullmatch(r"3\s+(\d+)", UP)
    m_id  = re.fullmatch(r"(?:ID|#)\s*(\d+)", UP)
    m_only_digits = re.fullmatch(r"\d{2,}", UP)  # si manda solo dígitos (>=2) tratamos como ID
    pid = None
    if m_id3: pid = int(m_id3.group(1))
    elif m_id: pid = int(m_id.group(1))
    elif m_only_digits: pid = int(m_only_digits.group(0))
    if pid is not None:
        row = _fetch_pedido_by_id(prov_id, pid)
        if row:
            send_text(from_no_plus, _pretty_detail(row))
        else:
            send_text(from_no_plus, f"🔎 No encuentro *ID {pid}* vinculado a tu CUIT.\nEscribí `menu` para ver opciones.")
        return {"ok": True}

    # ===== Fallback: si no entendemos, mostramos el menú lindo =====
    send_text(from_no_plus, "🤖 No entendí tu mensaje.\n\n" + _pretty_menu())
    return {"ok": True}
