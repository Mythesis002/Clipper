        output.unlink(missing_ok=True)
        detail = (result.stderr or "")[-500:]
        raise RuntimeError("Instagram compatibility re-encode failed: " + detail)
    media = probe_media(output)
    issues = instagram_media_issues(media)
    if (media.get("w"), media.get("h")) != (1080, 1920):
        issues.append("normalised output is not 1080x1920")
    if issues:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            "normalised Instagram media did not pass validation: "
            + "; ".join(issues) + " (" + media_summary(media) + ")"
        )
    log.warning("created Instagram compatibility encode: %s", media_summary(media))
    return output


def upload_reel_container(cfg, video_path, caption, access_token=None, ig_user_id=None):
    """Upload and process a Reel container, but do NOT publish it.

    Shared by the production publisher and the check-live smoke test. A FINISHED
    container is still unpublished until media_publish is explicitly called.
    """
    ig_id = ig_user_id or env_value("IG_USER_ID")
    token = access_token or env_value("IG_ACCESS_TOKEN")
    if not ig_id or not token:
        raise RuntimeError("Instagram upload requires non-empty IG_USER_ID and IG_ACCESS_TOKEN secrets")
    path = Path(video_path)
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError(f"Instagram upload input is missing or empty: {path}")
    g = graph(cfg)

    def upload_and_wait(candidate_path):
        response = requests.post(f"{g}/{ig_id}/media", data={
            "media_type": "REELS", "upload_type": "resumable", "caption": caption,
            "share_to_feed": "true", "access_token": token}, timeout=60)
        if not response.ok:
            raise RuntimeError(
                f"container create failed: HTTP {response.status_code} {response.text[:300]}")
        container = _instagram_error_payload(response)
        if not container:
            try:
                container = response.json()
            except ValueError as ex:
                raise RuntimeError("Instagram container create returned invalid JSON") from ex
        container_id, upload_uri = container.get("id"), container.get("uri")
        if not container_id or not upload_uri:
            raise RuntimeError(f"Instagram container response missing id or upload URI: {container}")

        data = candidate_path.read_bytes()
        upload = requests.post(upload_uri, data=data, timeout=600, headers={
            "Authorization": f"OAuth {token}", "offset": "0",
            "file_size": str(len(data)), "Content-Type": "application/octet-stream"})
        if not upload.ok:
            error = f"HTTP {upload.status_code} {upload.text[:400]}"
            if _instagram_error_type(upload) == "ProcessingFailedError":
                raise InstagramProcessingFailedError(error)
            raise RuntimeError("Instagram resumable upload failed: " + error)

        for _ in range(72):
            status_response = requests.get(f"{g}/{container_id}", timeout=30, params={
                "fields": "status_code,status", "access_token": token})
            if not status_response.ok:
                raise RuntimeError(
                    f"Instagram processing status failed: HTTP {status_response.status_code} "
                    f"{status_response.text[:200]}")
            try:
                status = status_response.json()
            except ValueError as ex:
                raise RuntimeError("Instagram processing status returned invalid JSON") from ex
            status_code = status.get("status_code")
            if status_code == "FINISHED":
                return str(container_id)
            if status_code in ("ERROR", "EXPIRED"):
                error = json.dumps(status, ensure_ascii=False)[:400]
                if "ProcessingFailedError" in error:
                    raise InstagramProcessingFailedError(error)
                raise RuntimeError(f"Instagram processing failed: {error}")
            if status_code not in ("IN_PROGRESS", "PENDING"):
                raise RuntimeError(f"Instagram returned unexpected container status: {status}")
            time.sleep(5)
        raise TimeoutError("Instagram processing timed out")

    try:
        return upload_and_wait(path)
    except InstagramProcessingFailedError as original_error:
        # The original container cannot be repaired in-place. Re-encode the file and
        # create a fresh container once; never retry the same rejected bytes.
        try:
            original_media = probe_media(path)
            log.warning("Instagram rejected original upload (%s); media=%s",
                        str(original_error)[:300], media_summary(original_media))
        except Exception:
            log.warning("Instagram rejected original upload (%s)", str(original_error)[:300])
        compatible = None
        try:
            compatible = normalize_reel_for_instagram(path)
            return upload_and_wait(compatible)
        except Exception as fallback_error:
            raise RuntimeError(
                "Instagram rejected the original file and the single compatibility retry failed. "
                f"Original rejection: {str(original_error)[:250]}; "
                f"fallback: {str(fallback_error)[:400]}"
            ) from fallback_error
        finally:
            if compatible is not None:
                compatible.unlink(missing_ok=True)


def post_reel(cfg, video_path, caption):
    container_id = upload_reel_container(cfg, video_path, caption)
    ig_id, token = env_value("IG_USER_ID"), env_value("IG_ACCESS_TOKEN")
    g = graph(cfg)
    try:
        p = requests.post(f"{g}/{ig_id}/media_publish", timeout=60, data={
            "creation_id": container_id, "access_token": token})
    except requests.RequestException as ex:
        raise PublishUncertainError(f"media_publish request outcome unknown: {ex}") from ex
    if p.status_code >= 500:
        raise PublishUncertainError(f"media_publish server error may have an unknown outcome: HTTP {p.status_code} {p.text[:200]}")
    if not p.ok:
        raise RuntimeError(f"publish rejected by Instagram: HTTP {p.status_code} {p.text[:300]}")
    try:
        media_id = p.json().get("id")
    except (ValueError, AttributeError):
        media_id = None
    if not media_id:
        raise PublishUncertainError("Instagram accepted publish request but returned no media id")
    return media_id


def due_slot(cfg, state, now=None):
    """The posting slot that still needs a post -> (slot_datetime, seconds_until_slot), or None.
    Looks `max_wait_min` ahead (the runner starts early and waits) and `catchup_hours` back, so a late,
    skipped or failed GitHub cron run is healed by the next one. A post near a slot counts as that slot's post."""
    tz = ZoneInfo(cfg["timezone"])
    now = now or datetime.now(tz)
    sch = cfg["schedule"]
    wait_s, catch_s = sch["max_wait_min"] * 60, sch["catchup_hours"] * 3600
    posted = []
    for c in state["clips"]:
        stamp = c.get("posted_at") or c.get("publish_started_at")