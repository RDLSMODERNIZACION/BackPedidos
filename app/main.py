import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routes import auth, pedidos

app = FastAPI(title="Dirac – Pedidos", version="1.0")

# CORS (abrimos para dev; ajustá dominios en prod)
origins = os.getenv("CORS_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins if origins != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(pedidos.router)

@app.get("/health")
def health():
    return {"ok": True}
