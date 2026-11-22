"""YouTube publisher for the Vale series.

Modes
  verify   - check the OAuth token and that it belongs to the expected channel (no changes)
  dry-run  - validate metadata, schedule, media checksums; print what `queue` would do (no changes)
  pilot    - upload one item as PRIVATE with no publish date, so the owner can review it in Studio
  queue    - upload items whose publish time is within `upload_ahead_days`, as private + scheduled publishAt;
             also schedules the reviewed pilot instead of uploading it twice, and sets thumbnails
  report   - refresh reports/REPORT.md (see report.py)

Secrets come from env: YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN; expected channel in YT_CHANNEL_ID.
They are never printed.
"""
import argparse, datetime as dt, hashlib, json, os, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda *a: os.path.join(ROOT, *a)
MEDIA = os.environ.get("MEDIA_DIR", P("media", "files"))
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]


def load(name):
    with open(P("publish", name), encoding="utf-8") as f:
        return json.load(f)


def save_ledger(ledger):
    tmp = P("publish", "ledger.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2, ensure_ascii=False)
    os.replace(tmp, P("publish", "ledger.json"))


def now():
    return dt.datetime.now(dt.timezone.utc)


def parse(ts):
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def log(*a):
    print(*a, flush=True)


# ---------- auth ----------
def youtube():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google.auth.exceptions import RefreshError
    from googleapiclient.discovery import build
    missing = [k for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN") if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing secrets: {', '.join(missing)}. Add them in GitHub → Settings → Secrets and variables → Actions.")
    creds = Credentials(None, refresh_token=os.environ["YT_REFRESH_TOKEN"], client_id=os.environ["YT_CLIENT_ID"],
                        client_secret=os.environ["YT_CLIENT_SECRET"], token_uri="https://oauth2.googleapis.com/token",
                        scopes=SCOPES)
    try:
        creds.refresh(Request())
    except RefreshError as e:
        if "invalid_grant" in str(e):
            sys.exit("TOKEN EXPIRED OR REVOKED (invalid_grant). Create a new refresh token (README, step 3) "
                     "and replace the YT_REFRESH_TOKEN secret. Nothing was uploaded.")
        raise
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def my_channel(yt):
    r = yt.channels().list(part="id,snippet,contentDetails", mine=True).execute()
    if not r.get("items"):
        sys.exit("This Google account has no YouTube channel.")
    ch = r["items"][0]
    exp = os.environ.get("YT_CHANNEL_ID", "").strip()
    log(f"Authorized channel: {ch['snippet']['title']} ({ch['id']})")
    if exp and exp != ch["id"]:
        sys.exit(f"WRONG CHANNEL: token belongs to {ch['id']}, expected {exp}. Nothing was uploaded.")
    if not exp:
        log("NOTE: repository variable YT_CHANNEL_ID is not set; set it to the ID above to lock uploads to this channel.")
    return ch


# ---------- media ----------
def checksums():
    out = {}
    with open(P("media", "checksums.sha256")) as f:
        for line in f:
            if line.strip():
                h, name = line.split()
                out[name] = h
    return out


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def ensure_media(name, settings, sums):
    """Episodes are stored in the repo (media/files). The full movie is too big for git, so it is
    rebuilt from the verified episodes with a lossless stream copy and checked by duration."""
    path = os.path.join(MEDIA, name)
    if name in sums:
        if not os.path.exists(path):
            sys.exit(f"MISSING FILE {path}. Nothing was uploaded.")
        got = sha256(path)
        if got != sums[name]:
            sys.exit(f"CHECKSUM MISMATCH for {name}: {got} != {sums[name]}. Nothing was uploaded.")
        return path
    mv = settings["movie"]
    if name != mv["file"]:
        sys.exit(f"{name} is neither in media/checksums.sha256 nor the movie. Nothing was uploaded.")
    import shutil, tempfile
    if not shutil.which("ffmpeg"):
        subprocess.run(["sudo", "apt-get", "install", "-y", "-qq", "ffmpeg"], check=True,
                       stdout=subprocess.DEVNULL)
    parts = [ensure_media(p, settings, sums) for p in mv["parts"]]
    out = os.path.join(tempfile.gettempdir(), name)
    lst = out + ".txt"
    with open(lst, "w") as f:
        f.writelines(f"file '{os.path.abspath(p)}'\n" for p in parts)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
                    "-movflags", "+faststart", out], check=True)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", out],
                               capture_output=True, text=True, check=True).stdout)
    if abs(dur - mv["duration_s"]) > 1.0:
        sys.exit(f"MOVIE BUILD FAILED: duration {dur:.2f}s, expected {mv['duration_s']}s. Nothing was uploaded.")
    log(f"Movie rebuilt from {len(parts)} episodes: {dur:.2f}s")
    return out


# ---------- YouTube operations ----------
def body_for(item, common, publish_at=None):
    status = {"privacyStatus": "private", "selfDeclaredMadeForKids": common["selfDeclaredMadeForKids"],
              "containsSyntheticMedia": common["containsSyntheticMedia"], "license": common["license"],
              "embeddable": common["embeddable"]}
    if publish_at:
        status["publishAt"] = publish_at
    return {"snippet": {"title": item["title"], "description": item["description"], "tags": item["tags"],
                        "categoryId": common["categoryId"], "defaultLanguage": common["defaultLanguage"],
                        "defaultAudioLanguage": common["defaultAudioLanguage"]},
            "status": status}


def upload(yt, path, body):
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError
    media = MediaFileUpload(path, mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media, notifySubscribers=True)
    resp, tries = None, 0
    while resp is None:
        try:
            st, resp = req.next_chunk()
            if st:
                log(f"  {int(st.progress() * 100)}%")
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504) and tries < 6:
                tries += 1; time.sleep(2 ** tries); continue
            raise
    return resp


def find_by_marker(yt, ch, marker):
    """Reconcile after a crash/timeout: look for our marker tag among the latest uploads."""
    pl = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    ids = [i["contentDetails"]["videoId"] for i in
           yt.playlistItems().list(part="contentDetails", playlistId=pl, maxResults=50).execute().get("items", [])]
    if not ids:
        return None
    for v in yt.videos().list(part="snippet,status", id=",".join(ids)).execute().get("items", []):
        if marker in v["snippet"].get("tags", []):
            return v
    return None


def set_thumbnail_if_ready(yt, item, entry):
    from googleapiclient.http import MediaFileUpload
    thumb = P(item["thumbnail"])
    if entry.get("thumbnail_set") or not entry.get("videoId") or not os.path.exists(thumb):
        return False
    v = yt.videos().list(part="processingDetails", id=entry["videoId"]).execute().get("items", [])
    status = v[0].get("processingDetails", {}).get("processingStatus") if v else None
    if status != "succeeded":
        log(f"  {item['key']}: processing={status}, thumbnail later")
        return False
    yt.thumbnails().set(videoId=entry["videoId"], media_body=MediaFileUpload(thumb)).execute()
    entry["thumbnail_set"] = now().isoformat()
    log(f"  {item['key']}: thumbnail set")
    return True


def video_health(yt, ids):
    """Status of our videos incl. Content ID blocks (regionRestriction) and rejections."""
    out = {}
    for i in range(0, len(ids), 50):
        for v in yt.videos().list(part="status,contentDetails,processingDetails",
                                  id=",".join(ids[i:i + 50])).execute().get("items", []):
            st, cd = v.get("status", {}), v.get("contentDetails", {})
            rr = cd.get("regionRestriction", {})
            blocked_n = len(rr.get("blocked", []))
            allowed = rr.get("allowed")
            blocked = (allowed is not None and len(allowed) == 0) or blocked_n >= 100 \
                or st.get("uploadStatus") == "rejected"
            partial = (not blocked) and (blocked_n > 0 or (allowed is not None and len(allowed) < 100))
            out[v["id"]] = {"privacy": st.get("privacyStatus"), "upload": st.get("uploadStatus"),
                            "publishAt": st.get("publishAt"), "rejection": st.get("rejectionReason"),
                            "processing": v.get("processingDetails", {}).get("processingStatus"),
                            "blocked_countries": blocked_n, "allowed_countries": None if allowed is None else len(allowed),
                            "verdict": "BLOCKED" if blocked else ("PARTIAL-BLOCK" if partial else "OK")}
    return out


def write_check(ledger, health):
    lines = [f"# Video check — {now():%Y-%m-%d %H:%M} UTC", "",
             "| Part | Verdict | Privacy | Upload | Processing | Publish at | Blocked in | Link |", "|---|---|---|---|---|---|---|---|"]
    for k, e in ledger.items():
        h = health.get(e.get("videoId"), {})
        if not e.get("videoId"):
            continue
        where = "all countries" if h.get("verdict") == "BLOCKED" else (f"{h.get('blocked_countries')} countries" if h.get("blocked_countries") else "–")
        lines.append(f"| {k} | **{h.get('verdict', 'NOT FOUND')}** | {h.get('privacy')} | {h.get('upload')} | {h.get('processing')} | "
                     f"{h.get('publishAt') or '–'} | {where} | https://youtu.be/{e['videoId']} |")
    lines += ["", "BLOCKED = Content ID or rejection blocks the video everywhere. The queue unschedules such videos automatically."]
    os.makedirs(P("reports"), exist_ok=True)
    with open(P("reports", "CHECK.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log("\n".join(lines))


# ---------- validation ----------
def validate(meta, sched, settings, sums, need_files=False):
    items = {i["key"]: i for i in meta["items"]}
    errs = []
    for s in sched["items"]:
        it = items.get(s["key"])
        if not it: errs.append(f"schedule key {s['key']} has no metadata"); continue
        if len(it["title"]) > 100: errs.append(f"{s['key']}: title > 100 chars")
        if len(it["description"]) > 5000: errs.append(f"{s['key']}: description > 5000 chars")
        if "{CHAPTERS}" in it["description"]: errs.append(f"{s['key']}: description still has {{CHAPTERS}} placeholder")
        if it["file"] not in sums and it["file"] != settings.get("movie", {}).get("file"):
            errs.append(f"{s['key']}: {it['file']} missing from media/checksums.sha256")
        parse(s["publishAt"])
    if errs:
        sys.exit("VALIDATION FAILED:\n  " + "\n  ".join(errs))
    return items


# ---------- modes ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=["verify", "dry-run", "pilot", "queue", "report", "check"])
    ap.add_argument("--item", default=None)
    a = ap.parse_args()
    meta, sched, settings, ledger = load("metadata.json"), load("schedule.json"), load("settings.json"), load("ledger.json")
    sums = checksums()
    items = validate(meta, sched, settings, sums)
    common = meta["common"]

    if a.mode == "report":
        import report; report.main(); return

    if a.mode == "dry-run":
        log("Validation OK.")
        t = now()
        for s in sorted(sched["items"], key=lambda x: x["publishAt"]):
            e = ledger.get(s["key"], {})
            when = parse(s["publishAt"])
            state = e.get("state", "not uploaded")
            due = (when - t).total_seconds() / 86400 <= settings["upload_ahead_days"]
            log(f"{s['key']:6} {s['publishAt']}  {state:22} {'→ would upload now' if due and state == 'not uploaded' else ''}")
        if os.environ.get("YT_REFRESH_TOKEN"):
            my_channel(youtube())
        return

    yt = youtube()
    ch = my_channel(yt)
    if a.mode == "verify":
        log("Token OK."); return

    if a.mode == "check":
        ids = [e["videoId"] for e in ledger.values() if e.get("videoId")]
        write_check(ledger, video_health(yt, ids) if ids else {}); return

    if a.mode == "pilot":
        key = a.item or settings["pilot_key"]
        it = items[key]
        e = ledger.get(key, {})
        if e.get("videoId"):
            log(f"{key} already uploaded as {e['videoId']} ({e['state']}). Nothing to do."); return
        found = find_by_marker(yt, ch, it["tags"][-1])
        if found:
            ledger[key] = {"videoId": found["id"], "state": "pilot-private", "found_by_reconcile": True}
            save_ledger(ledger); log(f"Found existing upload {found['id']}; recorded, not uploading again."); return
        path = ensure_media(it["file"], settings, sums)
        ledger[key] = {"state": "uploading", "started": now().isoformat()}; save_ledger(ledger)
        resp = upload(yt, path, body_for(it, common))
        ledger[key] = {"videoId": resp["id"], "state": "pilot-private", "uploaded": now().isoformat()}
        save_ledger(ledger)
        log(f"PILOT uploaded as PRIVATE: https://youtu.be/{resp['id']}  (Studio: https://studio.youtube.com/video/{resp['id']}/edit)")
        return

    # queue
    if not settings.get("approved"):
        log("Waiting for owner approval (publish/settings.json → approved: false). Check the pilot, then set approved: true.")
    elif settings.get("paused"):
        log("Queue is PAUSED (publish/settings.json → paused: true). Nothing uploaded.")
    else:
        t = now(); done = 0
        for s in sorted(sched["items"], key=lambda x: x["publishAt"]):
            key, when = s["key"], parse(s["publishAt"])
            it, e = items[key], ledger.get(key, {})
            if e.get("state") in ("scheduled", "published", "missed", "blocked"):
                continue
            if (when - t).total_seconds() / 86400 > settings["upload_ahead_days"]:
                continue
            if (when - t).total_seconds() < settings["min_lead_minutes"] * 60:
                ledger[key] = {**e, "state": "missed", "note": f"publishAt {s['publishAt']} too close or past at {t.isoformat()}; edit schedule.json and set state back"}
                save_ledger(ledger); log(f"{key}: MISSED its slot — edit publish/schedule.json"); continue
            if done >= settings["max_uploads_per_run"]:
                break
            if e.get("state") == "uploading" and not e.get("videoId"):
                found = find_by_marker(yt, ch, it["tags"][-1])
                if found:
                    e = {"videoId": found["id"], "state": "pilot-private", "found_by_reconcile": True}
                else:
                    log(f"{key}: previous upload state unknown and not found on channel; retrying upload")
            if e.get("videoId") and video_health(yt, [e["videoId"]]).get(e["videoId"], {}).get("verdict") == "BLOCKED":
                ledger[key] = {**e, "state": "blocked", "note": "Content ID / rejection blocks it worldwide; not scheduled"}
                save_ledger(ledger); log(f"{key}: BLOCKED on YouTube — not scheduling"); continue
            if e.get("videoId"):
                # already on YouTube (pilot or reconciled): schedule it instead of uploading again
                v = yt.videos().list(part="status", id=e["videoId"]).execute()["items"][0]
                st = v["status"]; st["privacyStatus"] = "private"; st["publishAt"] = s["publishAt"]
                for k in ("uploadStatus", "failureReason", "rejectionReason", "madeForKids"):
                    st.pop(k, None)
                st["selfDeclaredMadeForKids"] = common["selfDeclaredMadeForKids"]
                st["containsSyntheticMedia"] = common["containsSyntheticMedia"]
                r = yt.videos().update(part="status", body={"id": e["videoId"], "status": st}).execute()
                got = r["status"].get("publishAt")
            else:
                found = find_by_marker(yt, ch, it["tags"][-1])
                if found:
                    log(f"{key}: already on channel as {found['id']} — not uploading twice")
                    ledger[key] = {"videoId": found["id"], "state": "pilot-private", "found_by_reconcile": True}
                    save_ledger(ledger); continue
                path = ensure_media(it["file"], settings, sums)
                ledger[key] = {"state": "uploading", "started": now().isoformat()}; save_ledger(ledger)
                r = upload(yt, path, body_for(it, common, s["publishAt"]))
                got = r["status"].get("publishAt")
            ok = got and parse(got) == when
            ledger[key] = {"videoId": r["id"], "state": "scheduled" if ok else "check-in-studio",
                           "publishAt": s["publishAt"], "youtube_publishAt": got, "uploaded": now().isoformat()}
            save_ledger(ledger); done += 1
            log(f"{key}: {'scheduled' if ok else 'UPLOADED BUT publishAt NOT CONFIRMED'} {got}  https://youtu.be/{r['id']}")

    # unschedule anything that got blocked after upload (Content ID runs after processing)
    waiting = [k for k, e in ledger.items() if e.get("state") in ("scheduled", "check-in-studio") and e.get("videoId")]
    if waiting:
        h = video_health(yt, [ledger[k]["videoId"] for k in waiting])
        for k in waiting:
            if h.get(ledger[k]["videoId"], {}).get("verdict") == "BLOCKED":
                yt.videos().update(part="status", body={"id": ledger[k]["videoId"], "status": {
                    "privacyStatus": "private", "selfDeclaredMadeForKids": common["selfDeclaredMadeForKids"],
                    "containsSyntheticMedia": common["containsSyntheticMedia"]}}).execute()
                ledger[k].update(state="blocked", note="blocked worldwide after upload; schedule removed, stays private")
                save_ledger(ledger); log(f"{k}: BLOCKED — publish date removed, video stays private")
        ids = [e["videoId"] for e in ledger.values() if e.get("videoId")]
        write_check(ledger, video_health(yt, ids))

    # thumbnails for anything already on YouTube
    for key, e in ledger.items():
        if key in items:
            try:
                if set_thumbnail_if_ready(yt, items[key], e): save_ledger(ledger)
            except Exception as ex:
                log(f"  {key}: thumbnail failed: {ex}")
    # mark scheduled items whose time has passed as published (verified via API)
    due = [k for k, e in ledger.items() if e.get("state") == "scheduled" and parse(e["publishAt"]) <= now()]
    if due:
        vs = yt.videos().list(part="status", id=",".join(ledger[k]["videoId"] for k in due)).execute()["items"]
        pub = {v["id"]: v["status"]["privacyStatus"] for v in vs}
        for k in due:
            if pub.get(ledger[k]["videoId"]) == "public":
                ledger[k]["state"] = "published"
        save_ledger(ledger)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
