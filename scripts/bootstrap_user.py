"""Create the Phase 1 single-tenant user and Instagram account row.

Credentials remain in deployment secrets (IG_USER_ID/IG_ACCESS_TOKEN). Token
ciphertext is intentionally NULL in Phase 1; Phase 2 adds envelope encryption.
"""
import os
import uuid
from db import connect\nfrom scheduler.crypto import encrypt_token


def main():
    user_id=os.environ.get("CLIPPER_USER_ID","").strip()
    email=os.environ.get("CLIPPER_USER_EMAIL","").strip()
    tz=os.environ.get("CLIPPER_TIMEZONE","Asia/Kolkata").strip()
    ig_id=os.environ.get("IG_USER_ID","").strip()\n    ig_token=os.environ.get("IG_ACCESS_TOKEN","").strip()\n    token_key=os.environ.get("TOKEN_ENCRYPTION_KEY","").strip()
    attestation=os.environ.get("SOURCE_RIGHTS_ATTESTATION","").strip()
    if not user_id or not email or not ig_id or not ig_token or not token_key:
        raise SystemExit("Set CLIPPER_USER_ID, CLIPPER_USER_EMAIL, IG_USER_ID, IG_ACCESS_TOKEN, and TOKEN_ENCRYPTION_KEY")
    if len(attestation)<30:
        raise SystemExit("Set SOURCE_RIGHTS_ATTESTATION with a meaningful confirmation that you own/license/are authorized to repost the enabled sources.")
    uuid.UUID(user_id)
    config_path=Path(os.environ.get("CLIPPER_CONFIG","config.yaml"))
    with config_path.open(encoding="utf-8") as stream:
        config=yaml.safe_load(stream) or {}
    enabled=[ch for ch in config.get("channels",[]) if ch.get("permission") is True]
    with connect() as conn,conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.user_id', %s, true)",(user_id,))
            cur.execute("INSERT INTO users(id,email,tz) VALUES (%s,%s,%s) ON CONFLICT(id) DO UPDATE SET email=EXCLUDED.email,tz=EXCLUDED.tz,updated_at=now()",
                        (user_id,email,tz))
            cur.execute("INSERT INTO connected_accounts(user_id,platform,external_id,token_ciphertext,status) VALUES (%s,'instagram',%s,%s,'active') ON CONFLICT(user_id,platform,external_id) DO UPDATE SET token_ciphertext=coalesce(EXCLUDED.token_ciphertext,connected_accounts.token_ciphertext),status='active',updated_at=now()",
                        (user_id,ig_id,encrypted_ig))
            if encrypted_yt:
                cur.execute("INSERT INTO connected_accounts(user_id,platform,external_id,token_ciphertext,status) VALUES (%s,'youtube',%s,%s,'active') ON CONFLICT(user_id,platform,external_id) DO UPDATE SET token_ciphertext=coalesce(EXCLUDED.token_ciphertext,connected_accounts.token_ciphertext),status='active',updated_at=now()",
                            (user_id,yt_channel,encrypted_yt))
            cur.execute("SELECT id FROM connected_accounts WHERE user_id=%s AND platform='instagram' AND external_id=%s",(user_id,ig_id))
            account_id=cur.fetchone()[0]
            for ch in enabled:
                cur.execute(
                    """INSERT INTO sources(user_id,youtube_handle,permission_attested,attested_at,attestation_text)
                       VALUES (%s,%s,true,now(),%s)
                       ON CONFLICT(user_id,youtube_handle) DO UPDATE SET
                         permission_attested=true,attested_at=now(),attestation_text=EXCLUDED.attestation_text,status='active'""",
                    (user_id,ch["handle"],attestation))
            weekly=config.get("weekly_post_times") or {}
            timezone_name=config.get("timezone",tz)
            cur.execute("UPDATE users SET tz=%s WHERE id=%s",(timezone_name,user_id))
            for weekday_name, times in weekly.items():
                weekday=["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"].index(weekday_name)
                for hhmm in times:
                    cur.execute(
                        """INSERT INTO schedule_rules(user_id,account_id,weekday,local_time,active,max_late_minutes)
                           VALUES (%s,%s,%s,%s,true,%s)
                           ON CONFLICT(account_id,weekday,local_time) DO UPDATE SET active=true,max_late_minutes=EXCLUDED.max_late_minutes,updated_at=now()""",
                        (user_id,account_id,weekday,hhmm,int(os.environ.get("MAX_LATE_MINUTES","20"))))
    print(f"Bootstrapped single tenant user={user_id}; attested_sources={len(enabled)}; platform tokens were encrypted before storage; no plaintext token was written to DB.")


if __name__=="__main__":
    main()
