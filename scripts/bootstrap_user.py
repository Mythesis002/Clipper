"""Bootstrap a single-tenant Phase 1 user, account tokens, attested sources, and schedule rules.

Token values are encrypted with AES-256-GCM before being written to PostgreSQL.
The encryption key is a deployment secret and is not stored in the database.
"""
import os
import uuid
from pathlib import Path

import yaml

from db import connect
from scheduler.crypto import encrypt_token


def main():
    user_id=os.environ.get("CLIPPER_USER_ID","").strip()
    email=os.environ.get("CLIPPER_USER_EMAIL","").strip()
    tz=os.environ.get("CLIPPER_TIMEZONE","Asia/Kolkata").strip()
    ig_id=os.environ.get("IG_USER_ID","").strip()
    ig_token=os.environ.get("IG_ACCESS_TOKEN","").strip()
    encryption_key=os.environ.get("TOKEN_ENCRYPTION_KEY","").strip()
    attestation=os.environ.get("SOURCE_RIGHTS_ATTESTATION","").strip()

    if not all([user_id,email,ig_id,ig_token,encryption_key]):
        raise SystemExit("Set CLIPPER_USER_ID, CLIPPER_USER_EMAIL, IG_USER_ID, IG_ACCESS_TOKEN, and TOKEN_ENCRYPTION_KEY")
    if len(attestation)<30:
        raise SystemExit("Set SOURCE_RIGHTS_ATTESTATION with a truthful statement of rights for enabled sources.")
    uuid.UUID(user_id)
    encrypted_ig=encrypt_token(ig_token)

    yt_token=os.environ.get("YT_TOKEN_JSON","").strip()
    yt_channel=os.environ.get("YT_CHANNEL_ID","").strip()
    encrypted_yt=encrypt_token(yt_token) if yt_token else None
    if yt_token and not yt_channel:
        raise SystemExit("Set YT_CHANNEL_ID when YT_TOKEN_JSON is configured")

    config_path=Path(os.environ.get("CLIPPER_CONFIG","config.yaml"))
    with config_path.open(encoding="utf-8") as stream:
        config=yaml.safe_load(stream) or {}
    enabled=[ch for ch in config.get("channels",[]) if ch.get("permission") is True]
    timezone_name=config.get("timezone",tz)

    # Validate IANA zone before creating/updating account data.
    from zoneinfo import ZoneInfo
    ZoneInfo(timezone_name)

    with connect() as conn,conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.user_id',%s,true)",(user_id,))
            cur.execute(
                """INSERT INTO users(id,email,tz) VALUES (%s,%s,%s)
                   ON CONFLICT(id) DO UPDATE SET email=EXCLUDED.email,tz=EXCLUDED.tz,updated_at=now()""",
                (user_id,email,timezone_name))
            cur.execute(
                """INSERT INTO connected_accounts(user_id,platform,external_id,token_ciphertext,status)
                   VALUES (%s,'instagram',%s,%s,'active')
                   ON CONFLICT(user_id,platform,external_id) DO UPDATE SET
                     token_ciphertext=EXCLUDED.token_ciphertext,status='active',updated_at=now()""",
                (user_id,ig_id,encrypted_ig))
            if encrypted_yt:
                cur.execute(
                    """INSERT INTO connected_accounts(user_id,platform,external_id,token_ciphertext,status)
                       VALUES (%s,'youtube',%s,%s,'active')
                       ON CONFLICT(user_id,platform,external_id) DO UPDATE SET
                         token_ciphertext=EXCLUDED.token_ciphertext,status='active',updated_at=now()""",
                    (user_id,yt_channel,encrypted_yt))
            cur.execute(
                "SELECT id FROM connected_accounts WHERE user_id=%s AND platform='instagram' AND external_id=%s",
                (user_id,ig_id))
            account_id=cur.fetchone()[0]

            for channel in enabled:
                handle=str(channel.get("handle") or "").strip()
                if not handle:
                    raise ValueError(f"Enabled source {channel.get('name')} has no YouTube handle")
                source_attestation=f"{attestation} [source={channel.get('name','')} ({handle})]"
                cur.execute(
                    """INSERT INTO sources(user_id,youtube_handle,permission_attested,attested_at,attestation_text)
                       VALUES (%s,%s,true,now(),%s)
                       ON CONFLICT(user_id,youtube_handle) DO UPDATE SET
                         permission_attested=true,attested_at=now(),
                         attestation_text=EXCLUDED.attestation_text,status='active'""",
                    (user_id,handle,source_attestation))

            weekly=config.get("weekly_post_times") or {}
            for weekday_name,times in weekly.items():
                weekday=["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"].index(weekday_name)
                for hhmm in times:
                    cur.execute(
                        """INSERT INTO schedule_rules(user_id,account_id,weekday,local_time,active,max_late_minutes)
                           VALUES (%s,%s,%s,%s,true,%s)
                           ON CONFLICT(account_id,weekday,local_time) DO UPDATE SET
                             active=true,max_late_minutes=EXCLUDED.max_late_minutes,updated_at=now()""",
                        (user_id,account_id,weekday,hhmm,int(os.environ.get("MAX_LATE_MINUTES","20"))))

    print(f"Bootstrapped user={user_id}; attested_sources={len(enabled)}; token plaintext was not written to PostgreSQL.")


if __name__=="__main__":
    main()
