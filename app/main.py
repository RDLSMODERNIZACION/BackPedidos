# app/main.py
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.routing import APIRoute

from app.routes import auth, pedidos, ui, vlateral, wsp, archivos, proveedores
from app.db import open_pool, close_pool, get_pool  # ✅ pool único

APP_NAME = "Dirac – Pedidos API"
APP_VER = "1.0"

app = FastAPI(title=APP_NAME, version=APP_VER)

# =========================
# Pool lifecycle
# =========================
@app.on_event("startup")
def _on_startup():
    # abre 1 solo pool global para todo el proceso (evita MaxClientsInSessionMode)
    open_pool()

@app.on_event("shutdown")
def _on_shutdown():
    # cierra el pool prolijo
    close_pool()

# =========================
# CORS
# =========================
# CORS_ORIGINS puede ser:
#   - "*" (comodín)
#   - lista separada por comas (https://foo.com,https://bar.com)
origins_env = (os.getenv("CORS_ORIGINS", "*") or "").strip()
if origins_env == "*":
    allow_origins = ["*"]
    # con "*" no se pueden credenciales; no hace falta para Authorization header
    allow_credentials = False
else:
    allow_origins = [o.strip() for o in origins_env.split(",") if o.strip()]
    allow_credentials = True

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=allow_credentials,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    # Importante: permitir Authorization y X-User para tus endpoints de review/upload
    allow_headers=["Authorization", "Content-Type", "X-User", "Accept", "Origin"],
    expose_headers=["Content-Disposition"],  # útil para descargas con nombre
)

# =========================
# Archivos estáticos
# =========================
# Directorio local (si llegás a servir archivos locales además de Supabase)
FILES_DIR = os.getenv("FILES_DIR", "files")
os.makedirs(FILES_DIR, exist_ok=True)
app.mount("/files", StaticFiles(directory=FILES_DIR), name="files")

# =========================
# Routers (el orden puede importar)
# =========================
# 1) Rutas estáticas/deterministas
app.include_router(wsp.router)          # /wsp/... (WhatsApp: webhook y pruebas)
app.include_router(vlateral.router)     # /ui/... (vistas de lectura)

# 2) Rutas de negocio
app.include_router(archivos.router)     # /archivos/... (subir/listar/review/firmar/descargar)
app.include_router(ui.router)           # /ui/pedidos/list, /ui/pedidos/{id}/info, etc.
app.include_router(pedidos.router)      # /pedidos (creación y operaciones de pedidos)
app.include_router(proveedores.router)  # /proveedores (buscar por CUIT, upsert, agregar a pedido)
app.include_router(auth.router)         # /auth (si aplica)

# =========================
# Health & Root
# =========================
@app.get("/")
def root():
    return {"ok": True, "service": APP_NAME, "version": APP_VER}

@app.get("/health")
def health():
    return {"ok": True}

# (Opcional) ping a DB y ver tope del pool
@app.get("/__db_ping")
def db_ping():
    try:
        with get_pool().connection() as con, con.cursor() as cur:
            cur.execute("select 1")
            one = cur.fetchone()
        return {"ok": True, "one": int(one[0]), "pool_max": get_pool().max_size}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# =========================
# Diagnóstico de rutas (opcional)
# =========================
@app.get("/__routes")
def list_routes():
    out = []
    for r in app.router.routes:
        if isinstance(r, APIRoute):
            out.append({"path": r.path, "name": r.name, "methods": sorted(list(r.methods))})
    return out
