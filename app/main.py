# app/main.py
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles  # ⬅ para servir archivos
from app.routes import auth, pedidos, ui     # ✅ unificado en routes

app = FastAPI(title="Dirac – Pedidos", version="1.0")

# ===== CORS =====
origins_env = os.getenv("CORS_ORIGINS", "*")
origins = [o.strip() for o in origins_env.split(",")] if origins_env else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins if origins != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],   # incluye OPTIONS
    allow_headers=["*"],   # incluye Content-Type, Authorization, etc.
    expose_headers=["Content-Disposition"],  # útil si querés descargar con nombre
)

# ===== Archivos estáticos (PDFs firmados, anexos, etc.) =====
# Config por env: FILES_DIR=files (default)
FILES_DIR = os.getenv("FILES_DIR", "files")
os.makedirs(FILES_DIR, exist_ok=True)
# Servimos todo lo que quede grabado allí (p.ej. /files/pedidos/<id>/formal.pdf)
app.mount("/files", StaticFiles(directory=FILES_DIR), name="files")

# ===== Routers =====
app.include_router(auth.router)
app.include_router(pedidos.router)
app.include_router(ui.router)  # incluye /ui/pedidos/... y subida de formal_pdf

# ===== Health =====
@app.get("/")
def root():
    return {"ok": True, "service": "Dirac – Pedidos API", "version": "1.0"}

@app.get("/health")
def health():
    return {"ok": True}
