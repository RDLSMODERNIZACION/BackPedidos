# app/main.py
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routes import auth, pedidos

app = FastAPI(title="Dirac – Pedidos", version="1.0")

origins_env = os.getenv("CORS_ORIGINS", "*")
origins = [o.strip() for o in origins_env.split(",")] if origins_env else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins if origins != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(pedidos.router)

@app.get("/")            # 👈 agrega esto
def root():
    return {"ok": True, "service": "Dirac – Pedidos API", "version": "1.0"}

@app.get("/health")      # ya lo tenías, lo dejamos
def health():
    return {"ok": True}
