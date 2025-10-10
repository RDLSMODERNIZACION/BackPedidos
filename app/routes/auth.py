# routes/auth.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import get_conn
from psycopg.rows import dict_row
import os, time, uuid

router = APIRouter(prefix="/auth", tags=["auth"])

# Config JWT opcional
JWT_SECRET = os.getenv("JWT_SECRET")  # si no está, devolvemos token opaco
JWT_ALG    = "HS256"
JWT_EXP_S  = 60 * 60 * 8  # 8 hs

class LoginIn(BaseModel):
    username: str
    password: str

class LoginOutUser(BaseModel):
    username: str
    secretaria: str | None = None
    secretaria_id: int | None = None

class LoginOut(BaseModel):
    token: str
    user: LoginOutUser

def _make_token(payload: dict) -> str:
    if JWT_SECRET:
        try:
            from jose import jwt
            claims = {
                **payload,
                "exp": int(time.time()) + JWT_EXP_S,
                "iat": int(time.time()),
            }
            return jwt.encode(claims, JWT_SECRET, algorithm=JWT_ALG)
        except Exception:
            pass
    # Fallback: token opaco
    return f"opaque.{uuid.uuid4()}"

@router.post("/login", response_model=LoginOut)
def login(body: LoginIn):
    username = body.username.strip()
    password = body.password

    if not username or not password:
        raise HTTPException(status_code=400, detail="Faltan credenciales")

    sql = """
    SELECT
      p.login_username AS username,
      p.secretaria_id,
      s.nombre AS secretaria
    FROM public.perfil p
    LEFT JOIN public.secretaria s ON s.id = p.secretaria_id
    WHERE p.is_active = TRUE
      AND p.login_username = %s
      AND p.password_hash = crypt(%s, p.password_hash);
    """
    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (username, password))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=401, detail="Usuario o contraseña inválidos")

            token = _make_token({
                "sub": row["username"],
                "sec_id": row["secretaria_id"],
            })

            return {
                "token": token,
                "user": {
                    "username": row["username"],
                    "secretaria": row["secretaria"],
                    "secretaria_id": row["secretaria_id"],
                },
            }
    except HTTPException:
        raise
    except Exception as e:
        # Evitá filtrar el detalle exacto; devolvemos un 500 legible
        raise HTTPException(status_code=500, detail=f"login_
