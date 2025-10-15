# app/main.py
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.routing import APIRoute

from app.routes import auth, pedidos, ui, vlateral, wsp, archivos, proveedores, pedidos_acciones
from app.db import open_pool, close_pool, get_pool  # ✅ pool único

APP_NAME = "Dirac – Pedidos API"
APP_VER = "1.0"

app = FastAPI(title=APP_NAME, version=APP_VER)

# =========================
# Pool lifecycle
# =========================
@app.on_event("startup")
def _on_startup():
    open_pool()  # abre 1 solo pool global

@app.on_event("shutdown")
def _on_shutdown():
    close_pool()  # cierra el pool prolijo

# =========================
# CORS
# =========================
origins_env = (os.getenv("CORS_ORIGINS", "*") or "").strip()
if origins_env == "*":
    allow_origins = ["*"]
    allow_credentials = False  # con "*" no se envían credenciales
else:
    allow_origins = [o.strip() for o in origins_env.split(",") if o.strip()]
    allow_credentials = True

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=allow_credentials,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-User", "Accept", "Origin"],
    expose_headers=["Content-Disposition"],
)

# =========================
# Archivos estáticos
# =========================
FILES_DIR = os.getenv("FILES_DIR", "files")
os.makedirs(FILES_DIR, exist_ok=True)
app.mount("/files", StaticFiles(directory=FILES_DIR), name="files")

# =========================
# Routers (el orden puede importar)
# =========================
# 1) Rutas estáticas/deterministas
app.include_router(wsp.router)              # /wsp/...
app.include_router(vlateral.router)         # /ui/...

# 2) Rutas de negocio
app.include_router(archivos.router)         # /archivos/...
app.include_router(ui.router)               # /ui/pedidos/...
app.include_router(pedidos.router)          # /pedidos (CRUD y operaciones de pedido)
app.include_router(pedidos_acciones.router) # /pedidos (decidir/patch)
app.include_router(proveedores.router)      # /proveedores
app.include_router(auth.router)             # /auth

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
