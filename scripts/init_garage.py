"""Generate local Garage config and secure S3 credentials for Docker Compose.

Production deployments should use a managed S3 endpoint (e.g. Cloudflare R2) and
secret manager instead of this local single-node Garage setup.
"""
import os
import secrets
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
ENV_FILE=ROOT/".env"
CONFIG_FILE=ROOT/"garage"/"garage.toml"


def _set_env_file(key,value):
    lines=ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    prefix=key+"="
    for i,line in enumerate(lines):
        if line.startswith(prefix):
            lines[i]=prefix+value
            break
    else:
        lines.append(prefix+value)
    ENV_FILE.write_text("\n".join(lines)+"\n")


def main():
    ENV_FILE.touch(exist_ok=True)
    rpc=os.environ.get("GARAGE_RPC_SECRET","").strip() or secrets.token_hex(32)
    if len(rpc)!=64 or any(ch not in "0123456789abcdefABCDEF" for ch in rpc):
        raise SystemExit("GARAGE_RPC_SECRET must be 64 hex characters")
    access=os.environ.get("S3_ACCESS_KEY_ID","").strip() or "GK"+secrets.token_hex(12)
    secret=os.environ.get("S3_SECRET_ACCESS_KEY","").strip() or secrets.token_hex(32)
    bucket=os.environ.get("S3_BUCKET","clipper-media").strip() or "clipper-media"
    if len(access)<8 or len(secret)<16:
        raise SystemExit("Garage access key must be >=8 chars and secret >=16 chars")
    for key,value in (("GARAGE_RPC_SECRET",rpc),("S3_ACCESS_KEY_ID",access),
                      ("S3_SECRET_ACCESS_KEY",secret),("S3_BUCKET",bucket),
                      ("S3_REGION","garage")):
        _set_env_file(key,value)
    CONFIG_FILE.parent.mkdir(parents=True,exist_ok=True)
    CONFIG_FILE.write_text(f"""metadata_dir = "/var/lib/garage/meta"
data_dir = "/var/lib/garage/data"
db_engine = "lmdb"
replication_factor = 1
rpc_bind_addr = "[::]:3901"
rpc_secret = "{rpc}"

[s3_api]
s3_region = "garage"
api_bind_addr = "[::]:3900"
""")
    CONFIG_FILE.chmod(0o600)
    ENV_FILE.chmod(0o600)
    print(f"Garage config written to {CONFIG_FILE}; .env updated with local S3 credentials.")


if __name__=="__main__":
    main()
