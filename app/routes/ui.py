# Debug de vistas (solo whitelist)
from typing import Literal
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg import Error as PsycopgError

@router.get("/debug/view/{which}")
def debug_view(which: Literal["eventos", "archivos"]):
    viewmap = {
        "eventos": "public.v_ui_pedido_eventos",
        "archivos": "public.v_ui_pedido_archivos",
    }
    view = viewmap[which]
    schema, name = view.split(".")

    try:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            # ¿Existe?
            cur.execute("SELECT to_regclass(%s) AS oid", (view,))
            exists = cur.fetchone()["oid"] is not None
            if not exists:
                return {
                    "exists": False,
                    "view": view,
                    "hint": f"Creá la vista con CREATE VIEW ... en {view}",
                }

            # Definición, owner y updatability
            cur.execute("""
                SELECT
                    pg_get_viewdef(%s::regclass, true) AS definition,
                    (SELECT relowner::regrole::text FROM pg_class WHERE oid = %s::regclass) AS owner
            """, (view, view))
            meta = cur.fetchone()

            cur.execute("""
                SELECT
                  is_updatable, is_insertable_into
                FROM information_schema.views
                WHERE table_schema = %s AND table_name = %s
            """, (schema, name))
            up = cur.fetchone()

            # Columnas
            cur.execute("""
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
            """, (schema, name))
            columns = cur.fetchall()

            # Conteo + 5 filas de muestra
            cur.execute(f"SELECT count(*) AS n FROM {view}")
            n = cur.fetchone()["n"]

            cur.execute(f"SELECT * FROM {view} ORDER BY 1 DESC LIMIT 5")
            sample = cur.fetchall()

            return {
                "exists": True,
                "view": view,
                "owner": meta["owner"],
                "is_updatable": (up or {}).get("is_updatable"),
                "is_insertable_into": (up or {}).get("is_insertable_into"),
                "columns": columns,
                "count": n,
                "sample": sample,
                "definition": meta["definition"],
            }
    except PsycopgError as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e.__class__.__name__}: {str(e).strip()}")
