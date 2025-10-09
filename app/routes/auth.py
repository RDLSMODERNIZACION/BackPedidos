from fastapi import APIRouter, HTTPException
from psycopg.rows import dict_row
from app.db import get_conn
from app.schemas import LoginIn, LoginOut

router = APIRouter(prefix="/auth", tags=["auth"])

@router.post("/login", response_model=LoginOut)
def login(body: LoginIn):
    # Sin citext: comparamos en lower()
    sql = """
    select
      pf.user_id::text,
      pf.nombre,
      pf.secretaria_id,
      s.nombre as secretaria
    from public.perfil pf
    left join public.secretaria s on s.id = pf.secretaria_id
    where pf.is_active = true
      and lower(pf.login_username::text) = lower(%s)
      and pf.password_plain = %s
    limit 1;
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (body.username, body.password))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="Usuario o contraseña inválidos")
        return {
            "user_id": row["user_id"],
            "nombre": row["nombre"],
            "secretaria_id": row["secretaria_id"],
            "secretaria": row["secretaria"],
        }
