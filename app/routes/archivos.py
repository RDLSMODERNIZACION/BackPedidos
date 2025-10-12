# app/routes/archivos.py
# Manejo de archivos (subir, listar, revisar, firmar/descargar) para TODOS los PDF:
# - presupuesto_1 / presupuesto_2  (si se APRUEBA → pedido enviado → aprobado)
# - anexo1_obra
# - formal_pdf                     (si se APRUEBA → en_proceso)
# - expediente_1 / expediente_2
#
# Versionado: cada upload crea SIEMPRE una nueva fila (no upsert).

from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Header
from fastapi.responses import RedirectResponse
from fastapi.encoders import jsonable_encoder
from typing import Optional, Any
from psycopg.rows import dict_row
from uuid import uuid4
import os
import httpx

from app.db import get_conn  # ajustá el import si tu paquete difiere

router = APIRouter(prefix="/archivos", tags=["archivos"])

# =========================
# Config Supabase Storage
# =========================
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "pedidos-prod")

# Permitidos: TODOS los tipos que usás en la tabla
ALLOWED_TIPO_DOC = {
    "presupuesto_1",
    "presupuesto_2",
    "anexo1_obra",
    "formal_pdf",
    "expediente_1",
    "expediente_2",
}

# =========================
# Helpers
# =========================
def _sb_object_path(pedido_id: int, tipo_doc: str, filename: str) -> str:
    safe = (filename or "archivo.pdf").replace("/", "_").replace("\\", "_").strip()
    return f"pedido_{pedido_id}/{tipo_doc}/{uuid4()}_{safe}"

def _iso(dt: Optional[Any]) -> Optional[str]:
    try:
        return dt.isoformat() if dt is not None else None
    except Exception:
        return dt

# =========================
# Upload (nueva versión SIEMPRE)
# =========================
@router.post("/{pedido_id}")
async def upload_archivo(
    pedido_id: int,
    tipo_doc: str = Form(...),           # cualquiera de ALLOWED_TIPO_DOC
    archivo: UploadFile = File(...),     # clave "archivo" en multipart/form-data
):
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=500, detail="Faltan SUPABASE_URL o SUPABASE_SERVICE_ROLE_KEY en el backend")

    if tipo_doc not in ALLOWED_TIPO_DOC:
        raise HTTPException(
            status_code=400,
            detail=f"tipo_doc inválido: {tipo_doc}. Permitidos: {', '.join(sorted(ALLOWED_TIPO_DOC))}"
        )

    if not archivo.filename:
        raise HTTPException(status_code=400, detail="Falta nombre de archivo")
    mime = archivo.content_type or "application/pdf"
    if mime != "application/pdf":
        raise HTTPException(status_code=415, detail=f"Solo se aceptan PDF. Recibido: {mime}")

    data: bytes = await archivo.read()
    if not data:
        raise HTTPException(status_code=400, detail="El archivo llegó vacío (0 bytes)")

    # Verificar pedido
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT 1 FROM public.pedido WHERE id = %s;", (pedido_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Pedido no encontrado")

    # Subir a Storage (uuid → versión nueva)
    object_key = _sb_object_path(pedido_id, tipo_doc, archivo.filename)
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{object_key}"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            upload_url,
            headers={
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": mime,
                "x-upsert": "true",  # el key es único por uuid → siempre versión nueva
            },
            content=data,
        )
        if r.status_code not in (200, 201):
            raise HTTPException(status_code=r.status_code, detail=f"Error subiendo a Storage: {r.text}")

    storage_path = f"supabase://{SUPABASE_BUCKET}/{object_key}"
    size = len(data)

    # Guardar metadatos (INSERT SIEMPRE → versión nueva)
    sql = """
    INSERT INTO public.pedido_archivo
      (pedido_id, storage_path, file_name, content_type, bytes, tipo_doc)
    VALUES
      (%s,        %s,           %s,        %s,           %s,    %s)
    RETURNING id, created_at;
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (pedido_id, storage_path, archivo.filename, mime, size, tipo_doc))
        row = cur.fetchone()
        conn.commit()

    return {"ok": True, "archivo_id": row["id"], "bytes": size, "path": storage_path, "uploaded_at": _iso(row["created_at"])}

# =========================
# Listar por pedido (vista)
# =========================
@router.get("/pedido/{pedido_id}")
def list_archivos_por_pedido(pedido_id: int):
    """Lista adjuntos del pedido desde v_ui_pedido_archivos (todas las versiones)."""
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT
                  id,
                  pedido_id,
                  kind,
                  filename,
                  content_type,
                  size_bytes,
                  uploaded_at,
                  review_status,
                  review_notes,
                  reviewed_by,
                  reviewed_at,
                  url
                FROM public.v_ui_pedido_archivos
               WHERE pedido_id = %s
               ORDER BY uploaded_at DESC NULLS LAST, id DESC
            """, (pedido_id,))
            rows = cur.fetchall() or []
        items = [{
            "id": r["id"],
            "pedido_id": r["pedido_id"],
            "kind": r["kind"],
            "filename": r["filename"],
            "content_type": r["content_type"],
            "size_bytes": r["size_bytes"],
            "uploaded_at": _iso(r["uploaded_at"]),
            "review_status": r["review_status"],
            "review_notes": r["review_notes"],
            "reviewed_by": r["reviewed_by"],
            "reviewed_at": _iso(r["reviewed_at"]),
            "url": r["url"],
        } for r in rows]
        return jsonable_encoder({"items": items})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"list_archivos_error: {e}")

# =========================
# Review (aprobado / observado)
# Reglas:
# - si decision == 'aprobado' y tipo_doc == 'presupuesto_*'  y estado actual == 'enviado'   → 'aprobado'
# - si decision == 'aprobado' y tipo_doc == 'formal_pdf'                                    → 'en_proceso'
# =========================
@router.post("/{archivo_id}/review")
def review_archivo(
    archivo_id: int,
    decision: str = Form(...),             # "aprobado" | "observado"
    notes: Optional[str] = Form(None),
    x_user: Optional[str] = Header(default=None),
):
    decision = (decision or "").strip().lower()
    if decision not in ("aprobado", "observado"):
        raise HTTPException(status_code=422, detail="decision debe ser 'aprobado' u 'observado'")

    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            # 0) Bloquear fila del archivo y traer pedido_id + tipo_doc
            cur.execute("""
                SELECT pedido_id, tipo_doc
                  FROM public.pedido_archivo
                 WHERE id = %s
                 FOR UPDATE
            """, (archivo_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Archivo no encontrado")

            pedido_id = row["pedido_id"]
            tipo_doc  = (row["tipo_doc"] or "").lower()

            # 1) Actualizar review_* del archivo
            cur.execute("""
                UPDATE public.pedido_archivo
                   SET review_status = %s,
                       review_notes  = %s,
                       reviewed_by   = %s,
                       reviewed_at   = now()
                 WHERE id = %s
             RETURNING id, pedido_id, review_status, review_notes, reviewed_by, reviewed_at
            """, (decision, (notes or None), (x_user or "ui"), archivo_id))
            rv = cur.fetchone()

            # 2) Transiciones según reglas
            if decision == "aprobado":
                # 2.a) presupuestos → enviado → aprobado
                if tipo_doc in ("presupuesto_1", "presupuesto_2"):
                    cur.execute("SELECT estado FROM public.pedido WHERE id = %s FOR UPDATE", (pedido_id,))
                    p = cur.fetchone()
                    if not p:
                        raise HTTPException(status_code=404, detail="Pedido no encontrado")
                    estado_anterior = p["estado"]
                    if estado_anterior == "enviado":
                        cur.execute("""
                            UPDATE public.pedido
                               SET estado = 'aprobado',
                                   updated_at = now()
                             WHERE id = %s
                         RETURNING updated_at
                        """, (pedido_id,))
                        _ = cur.fetchone()
                        cur.execute("""
                            INSERT INTO public.pedido_historial
                                (pedido_id, estado_anterior, estado_nuevo, motivo, changed_by)
                            VALUES (%s, %s, %s, %s, %s)
                        """, (
                            pedido_id,
                            estado_anterior,
                            "aprobado",
                            "aprobación de presupuesto",
                            x_user or "ui",
                        ))

                # 2.b) formal_pdf → en_proceso
                elif tipo_doc == "formal_pdf":
                    cur.execute("SELECT estado FROM public.pedido WHERE id = %s FOR UPDATE", (pedido_id,))
                    p = cur.fetchone()
                    if not p:
                        raise HTTPException(status_code=404, detail="Pedido no encontrado")
                    estado_anterior = p["estado"]
                    if estado_anterior != "en_proceso":
                        cur.execute("""
                            UPDATE public.pedido
                               SET estado = 'en_proceso',
                                   updated_at = now()
                             WHERE id = %s
                         RETURNING updated_at
                        """, (pedido_id,))
                        _ = cur.fetchone()
                        cur.execute("""
                            INSERT INTO public.pedido_historial
                                (pedido_id, estado_anterior, estado_nuevo, motivo, changed_by)
                            VALUES (%s, %s, %s, %s, %s)
                        """, (
                            pedido_id,
                            estado_anterior,
                            "en_proceso",
                            "aprobación de formal_pdf",
                            x_user or "ui",
                        ))

            conn.commit()

        return jsonable_encoder(rv)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"review_error: {e}")

# =========================
# URLs firmadas / descarga
# =========================
@router.get("/{archivo_id}/signed")
async def get_signed_download(archivo_id: int, expires_sec: int = 600):
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=500, detail="Faltan SUPABASE_URL o SUPABASE_SERVICE_ROLE_KEY en el backend")

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT storage_path, file_name, content_type
              FROM public.pedido_archivo
             WHERE id = %s
        """, (archivo_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Archivo no encontrado")

    storage_path: str = row["storage_path"] or ""
    if not storage_path.startswith("supabase://"):
        raise HTTPException(status_code=400, detail="Este archivo no está en Supabase Storage")

    _, bucket_and_key = storage_path.split("://", 1)
    bucket, key = bucket_and_key.split("/", 1)

    sign_url = f"{SUPABASE_URL}/storage/v1/object/sign/{bucket}/{key}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            sign_url,
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"},
            json={"expiresIn": expires_sec},
        )
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=f"Error firmando URL: {r.text}")
        payload = r.json()
        signed_url = f"{SUPABASE_URL}{payload['signedURL']}"

    return {
        "url": signed_url,
        "file_name": row["file_name"],
        "content_type": row["content_type"],
        "expires_in": expires_sec,
    }

@router.get("/{archivo_id}/download")
async def download_redirect(archivo_id: int, expires_sec: int = 600):
    """Redirige (307) a la Signed URL de Supabase."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=500, detail="Faltan SUPABASE_URL o SUPABASE_SERVICE_ROLE_KEY")

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT storage_path FROM public.pedido_archivo WHERE id=%s", (archivo_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Archivo no encontrado")

    storage_path: str = row["storage_path"] or ""
    if not storage_path.startswith("supabase://"):
        raise HTTPException(status_code=400, detail="Este archivo no está en Supabase Storage")

    _, bucket_and_key = storage_path.split("://", 1)
    bucket, key = bucket_and_key.split("/", 1)

    sign_url = f"{SUPABASE_URL}/storage/v1/object/sign/{bucket}/{key}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            sign_url,
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"},
            json={"expiresIn": expires_sec},
        )
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=f"Error firmando URL: {r.text}")
        payload = r.json()
        signed_url = f"{SUPABASE_URL}{payload['signedURL']}"

    return RedirectResponse(url=signed_url, status_code=307)
