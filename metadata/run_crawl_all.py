#!/usr/bin/env python3
"""Resumable batch crawler for SofaScore match metadata.

Walks the FIFA World Cup 2026 match list (``match_ids.txt``), and for every
finished match not already done, runs the per-match crawler + aggregator
(``crawl.py``) with source pruning, being polite with delays so the API does
not block us. Mirrors the design of ``videos/run_download_20.py``:

  * progress.json is the source of truth -> never re-crawls a finished match.
  * Existing data/<slug>/metadata.json also counts as done (resumable even if
    progress.json is lost).
  * Never stops because one match fails; records the error and continues.
  * Writes reports/report.md and logs/crawl_all.log at the end.

Layout (all under this metadata/ directory, git-ignored except match_ids.txt):
  data/<slug>/...        crawled output (one dir per match)
  progress.json          {completed, failed, skipped}
  reports/report.md
  logs/crawl_all.log

Config via env vars:
  PER_REQUEST_DELAY   seconds between API requests   (default 1.0, passed to Crawler)
  MATCH_DELAY         seconds to sleep between matches (default 10)
  LIMIT               stop after N newly-crawled matches (default: all)
  ONLY                only crawl slugs containing this substring (default: all)
  INCLUDE_NOTSTARTED  if "1", also crawl not-yet-played matches (default: skip)

Usage:
  python run_crawl_all.py                 # crawl all remaining finished matches
  python run_crawl_all.py --refresh-ids   # rebuild match_ids.txt from the API first
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

from crawl import API, BASE_HEADERS, IMPERSONATE, Crawler
from curl_cffi import requests

UNIQUE_TOURNAMENT_ID = 16
SEASON_ID = 58210

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
MATCH_IDS = os.path.join(ROOT, "match_ids.txt")
PROGRESS_JSON = os.path.join(ROOT, "progress.json")
LOGDIR = os.path.join(ROOT, "logs")
REPORTDIR = os.path.join(ROOT, "reports")
LOGFILE = os.path.join(LOGDIR, "crawl_all.log")

PER_REQUEST_DELAY = float(os.environ.get("PER_REQUEST_DELAY", "1.0"))
MATCH_DELAY = float(os.environ.get("MATCH_DELAY", "10"))
LIMIT = int(os.environ["LIMIT"]) if os.environ.get("LIMIT") else None
ONLY = os.environ.get("ONLY", "")
INCLUDE_NOTSTARTED = os.environ.get("INCLUDE_NOTSTARTED") == "1"


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat()}  {msg}"
    print(line, flush=True)
    os.makedirs(LOGDIR, exist_ok=True)
    with open(LOGFILE, "a") as fh:
        fh.write(line + "\n")


# --------------------------------------------------------------- match list
def fetch_season_events() -> list[dict]:
    """Return every event in the WC season (played + upcoming), de-duplicated."""
    s = requests.Session(impersonate=IMPERSONATE)
    s.headers.update(BASE_HEADERS)
    events: dict[int, dict] = {}
    for kind in ("last", "next"):
        page = 0
        while page < 50:
            url = (f"{API}/unique-tournament/{UNIQUE_TOURNAMENT_ID}/"
                   f"season/{SEASON_ID}/events/{kind}/{page}")
            r = s.get(url, timeout=20)
            if r.status_code != 200:
                break
            data = r.json()
            evs = data.get("events", [])
            new = sum(1 for e in evs if e["id"] not in events)
            events.update({e["id"]: e for e in evs})
            if not data.get("hasNextPage") or new == 0:
                break
            page += 1
            time.sleep(0.5)
    return list(events.values())


def write_match_ids(events: list[dict]) -> None:
    rows = []
    for e in events:
        h = (e.get("homeTeam") or {}).get("slug", "home")
        a = (e.get("awayTeam") or {}).get("slug", "away")
        slug = f"{h}_v_{a}".replace("-", "_")
        status = (e.get("status") or {}).get("type", "")
        rnd = (e.get("roundInfo") or {}).get("round", "")
        rows.append((e.get("startTimestamp", 0), e["id"], slug, status, rnd))
    rows.sort()
    lines = [f"# event_id\tslug\tstatus\tround   (FIFA World Cup 2026, "
             f"unique-tournament {UNIQUE_TOURNAMENT_ID}, season {SEASON_ID})"]
    lines += [f"{eid}\t{slug}\t{status}\t{rnd}" for _ts, eid, slug, status, rnd in rows]
    with open(MATCH_IDS, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    log(f"wrote {MATCH_IDS}: {len(rows)} matches")


def load_match_ids() -> list[dict]:
    """Parse match_ids.txt -> [{id, slug, status, round}]."""
    out = []
    with open(MATCH_IDS) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            eid = int(parts[0])
            slug = parts[1] if len(parts) > 1 else ""
            status = parts[2] if len(parts) > 2 else ""
            rnd = parts[3] if len(parts) > 3 else ""
            out.append({"id": eid, "slug": slug, "status": status, "round": rnd})
    return out


# ------------------------------------------------------------------ progress
def load_progress() -> dict:
    if os.path.exists(PROGRESS_JSON):
        with open(PROGRESS_JSON) as fh:
            p = json.load(fh)
    else:
        p = {}
    p.setdefault("completed", [])
    p.setdefault("failed", [])
    p.setdefault("skipped", [])
    return p


def save_progress(p: dict) -> None:
    with open(PROGRESS_JSON, "w") as fh:
        json.dump(p, fh, indent=2)


def already_done(slug: str) -> bool:
    return bool(slug) and os.path.exists(os.path.join(DATA, slug, "metadata.json"))


# ----------------------------------------------------------------- reporting
def write_report(progress: dict, matches: list[dict]) -> None:
    os.makedirs(REPORTDIR, exist_ok=True)
    by_id = {m["id"]: m for m in matches}
    lines = ["# SofaScore metadata crawl report", ""]
    lines.append(f"- Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"- Completed: **{len(progress['completed'])}**")
    lines.append(f"- Failed: **{len(progress['failed'])}**")
    lines.append(f"- Skipped: **{len(progress['skipped'])}**")
    lines.append("")
    lines.append("## Completed")
    for eid in progress["completed"]:
        m = by_id.get(eid, {})
        lines.append(f"- {eid}  {m.get('slug', '')}")
    if progress["failed"]:
        lines.append("")
        lines.append("## Failed")
        for f in progress["failed"]:
            lines.append(f"- {f.get('id')}  {f.get('error')}")
    with open(os.path.join(REPORTDIR, "report.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")


# ----------------------------------------------------------------------- run
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh-ids", action="store_true",
                    help="Rebuild match_ids.txt from the season API before crawling.")
    args = ap.parse_args()

    if args.refresh_ids or not os.path.exists(MATCH_IDS):
        log("refreshing match list from API ...")
        write_match_ids(fetch_season_events())

    matches = load_match_ids()
    progress = load_progress()

    # reconcile: any match with metadata.json already present is completed
    for m in matches:
        if already_done(m["slug"]) and m["id"] not in progress["completed"]:
            progress["completed"].append(m["id"])
    save_progress(progress)

    todo = []
    for m in matches:
        if m["id"] in progress["completed"]:
            continue
        if ONLY and ONLY not in m["slug"]:
            continue
        if m["status"] != "finished" and not INCLUDE_NOTSTARTED:
            if m["id"] not in progress["skipped"]:
                progress["skipped"].append(m["id"])
            continue
        todo.append(m)
    save_progress(progress)

    log(f"{len(matches)} matches total | already done {len(progress['completed'])} "
        f"| to crawl {len(todo)} | per_request_delay={PER_REQUEST_DELAY}s "
        f"match_delay={MATCH_DELAY}s limit={LIMIT}")

    crawled = 0
    for m in todo:
        if LIMIT is not None and crawled >= LIMIT:
            log(f"reached LIMIT={LIMIT}, stopping")
            break
        eid, slug = m["id"], m["slug"]
        log(f"==> crawling {eid} {slug}")
        try:
            Crawler(eid, out_base=DATA, delay=PER_REQUEST_DELAY,
                    prune_sources=True).run()
            progress["completed"].append(eid)
            # clear any prior failure record
            progress["failed"] = [f for f in progress["failed"] if f.get("id") != eid]
            log(f"    OK {slug}")
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            progress["failed"] = [f for f in progress["failed"] if f.get("id") != eid]
            progress["failed"].append({"id": eid, "slug": slug, "error": str(exc)})
            log(f"    FAILED {slug}: {exc}")
        save_progress(progress)
        crawled += 1
        if MATCH_DELAY:
            time.sleep(MATCH_DELAY)

    write_report(progress, matches)
    log(f"run done: completed {len(progress['completed'])} "
        f"failed {len(progress['failed'])} skipped {len(progress['skipped'])}")


if __name__ == "__main__":
    sys.exit(main())
