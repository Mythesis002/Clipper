"""Create the Phase 1 single-tenant user and Instagram account row.

Credentials remain in deployment secrets (IG_USER_ID/IG_ACCESS_TOKEN). Token
ciphertext is intentionally NULL in Phase 1; Phase 2 adds envelope encryption.
"""
import os
import uuid
from db import connect


def main():
    user_id=os.environ.get("CLIPPER_USER_ID","").strip()
    email=os.environ.get("CLIPPER_USER_EMAIL","").strip()
    tz=os.environ.get("CLIPPER_TIMEZONE","Asia/Kolkata").strip()
    ig_id=os.environ.get("IG_USER_ID","").strip()
    if not user_id or not email or not ig_id:
        raise SystemExit("Set CLIPPER_USER_ID, CLIPPER_USER_EMAIL, and IG_USER_ID")
    uuid.UUID(user_id)
    with connect() as conn,conn.transaction():
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users(id,email,tz) VALUES (%s,%s,%s) ON CONFLICT(id) DO UPDATE SET email=EXCLUDED.email,tz=EXCLUDED.tz,updated_at=now()",
                        (user_id,email,tz))
            cur.execute("INSERT INTO connected_accounts(user_id,platform,external_id,status) VALUES (%s,'instagram',%s,'active') ON CONFLICT(user_id,platform,external_id) DO NOTHING",
                        (user_id,ig_id))
    print(f"Bootstrapped single tenant user={user_id}; no access token was written to DB.")


if __name__=="__main__":
    main()
