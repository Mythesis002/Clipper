"""Import current JSON state into Postgres for a one-time single-user migration.

This script requires CLIPPER_USER_ID and uses the StateStore adapter. It does not
publish or run media work. Back up state/state.json before running.
"""
import os
from db import connect
from scheduler.legacy_adapter import JSON_STORE, save_state


def main():
    user_id=os.environ.get("CLIPPER_USER_ID","").strip()
    if not user_id: raise SystemExit("Set CLIPPER_USER_ID to the existing user UUID")
    state=JSON_STORE.load()
    os.environ['CLIPPER_STATE_BACKEND']='postgres'
    save_state(state)
    print(f"Imported legacy clips={len(state.get('clips',[]))}; no posts were published.")


if __name__=="__main__":
    main()
