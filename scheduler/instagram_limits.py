"""Fail-closed Instagram publishing quota check.

Meta's docs have had inconsistent static quota figures; use the account-specific
quota_total and quota_duration returned by content_publishing_limit rather than
hardcoding 50 or 100. Official reference:
https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/content_publishing_limit
"""
import requests


def assert_publish_quota_available(graph_version: str, ig_user_id: str, access_token: str) -> dict:
    if not graph_version or not ig_user_id or not access_token:
        raise RuntimeError("Graph version, IG user id, and token are required for quota check")
    response=requests.get(
        f"https://graph.facebook.com/{graph_version}/{ig_user_id}/content_publishing_limit",
        params={"fields":"quota_usage,config","access_token":access_token},
        timeout=(5,15),
    )
    if not response.ok:
        raise RuntimeError(f"Instagram quota endpoint HTTP {response.status_code}: {response.text[:200]}")
    try:
        payload=response.json()
    except ValueError as exc:
        raise RuntimeError("Instagram quota endpoint returned invalid JSON") from exc
    data=payload.get("data") if isinstance(payload,dict) else None
    if not isinstance(data,list) or not data or not isinstance(data[0],dict):
        raise RuntimeError("Instagram quota endpoint omitted data/quota_usage")
    item=data[0]
    config=item.get("config") or {}
    try:
        used=int(item["quota_usage"])
        total=int(config["quota_total"])
        duration=int(config.get("quota_duration",86400))
    except (KeyError,TypeError,ValueError) as exc:
        raise RuntimeError("Instagram quota response missing numeric quota_usage/config fields") from exc
    if used < 0 or total < 1 or duration < 1:
        raise RuntimeError("Instagram quota response contains invalid quota values")
    if used >= total:
        raise RuntimeError(f"Instagram publishing quota exhausted ({used}/{total} in {duration}s)")
    return {"quota_usage":used,"quota_total":total,"quota_duration":duration,"remaining":total-used}
