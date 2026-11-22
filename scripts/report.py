"""Writes reports/REPORT.md: queue state + public stats per video + channel totals. Read-only API calls."""
import datetime as dt, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda *a: os.path.join(ROOT, *a)


def main():
    from publish import youtube, load, parse
    meta, sched, ledger = load("metadata.json"), load("schedule.json"), load("ledger.json")
    items = {i["key"]: i for i in meta["items"]}
    yt = youtube()
    ch = yt.channels().list(part="statistics,snippet", mine=True).execute()["items"][0]
    ids = [e["videoId"] for e in ledger.values() if e.get("videoId")]
    stats = {}
    for i in range(0, len(ids), 50):
        for v in yt.videos().list(part="statistics,status", id=",".join(ids[i:i + 50])).execute().get("items", []):
            stats[v["id"]] = v
    now = dt.datetime.now(dt.timezone.utc)
    s = ch["statistics"]
    lines = [f"# {ch['snippet']['title']} — daily report",
             f"Updated: {now:%Y-%m-%d %H:%M} UTC. Data API counters are near-real-time; retention and engaged views are only in YouTube Studio → Analytics.",
             "", f"**Subscribers:** {s.get('subscriberCount', 'hidden')}  ·  **Channel views:** {s.get('viewCount')}  ·  **Videos:** {s.get('videoCount')}",
             "", "| Part | Status | Publish (UTC) | Views | Likes | Comments | Link |", "|---|---|---|---|---|---|---|"]
    for sc in sorted(sched["items"], key=lambda x: x["publishAt"]):
        k = sc["key"]; e = ledger.get(k, {}); vid = e.get("videoId")
        v = stats.get(vid, {}); st = v.get("statistics", {})
        status = v.get("status", {}).get("privacyStatus", e.get("state", "not uploaded")) if vid else "not uploaded"
        if vid and status == "private" and e.get("state") == "scheduled":
            status = "scheduled"
        lines.append(f"| {k} | {status} | {sc['publishAt'][:16].replace('T', ' ')} | {st.get('viewCount', '–')} | "
                     f"{st.get('likeCount', '–')} | {st.get('commentCount', '–')} | {'https://youtu.be/' + vid if vid else ''} |")
    pub = [(int(stats[e['videoId']]['statistics'].get('viewCount', 0)), k) for k, e in ledger.items()
           if e.get('videoId') in stats and stats[e['videoId']]['status']['privacyStatus'] == 'public']
    if len(pub) >= 3:
        top = sorted(pub, reverse=True)[:3]
        lines += ["", "**Top 3 by views:** " + ", ".join(f"{k} ({n})" for n, k in top),
                  "_Compare parts of the same age only; early parts have had more time._"]
    else:
        lines += ["", "_Fewer than 3 public videos — too early for conclusions._"]
    os.makedirs(P("reports"), exist_ok=True)
    with open(P("reports", "REPORT.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
