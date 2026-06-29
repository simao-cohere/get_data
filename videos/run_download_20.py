#!/usr/bin/env python3
"""Download the next N successful FIFA World Cup 2026 matches.

Implements the workflow described in AGENTS.md / download_next_20.md:

  * Build a name -> post-URL index by scraping the footballorgin.com category
    pages (handles the "v"/"vs" and team-name spelling differences).
  * Walk world_cup_matches.txt in order, skipping matches already completed.
  * For each match: download every available variant at full (720p) quality,
    verify it, then produce a compressed copy (480p, 30 fps, bitrate ~60% of
    the original), and verify that too.
  * Update progress.json after every match. Never stop because one match fails;
    retry transient failures, record, and continue.
  * Stop after TARGET_SUCCESSES new successful matches or when the list is
    exhausted, then write reports/report.md.

Layout:
  /data/1d/simao/football_downloads/downloads/<slug>/<variant>.mp4
  /data/1d/simao/football_downloads/downloads/<slug>/compressed/<variant>_480p30_h265.mp4
  (override with the DOWNLOADS env var)

  progress.json + progress/progress.json  (in videos/, next to this script)
  reports/report.md                       (in videos/, next to this script)
  logs/run.log                            (in videos/, next to this script)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone

import footballorgin_crawler as fc

ROOT = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS = os.environ.get(
    "DOWNLOADS",
    "/data/1d/simao/football_downloads/downloads",
)
LOGDIR = os.path.join(ROOT, "logs")
REPORTDIR = os.path.join(ROOT, "reports")
PROGRESSDIR = os.path.join(ROOT, "progress")
PROGRESS_JSON = os.path.join(ROOT, "progress.json")
MATCHES_TXT = os.path.join(ROOT, "world_cup_matches.txt")
LOGFILE = os.path.join(LOGDIR, "run.log")

TARGET_SUCCESSES = int(os.environ.get("TARGET_SUCCESSES", "20"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "6"))
MIN_FREE_GB = float(os.environ.get("MIN_FREE_GB", "15"))
COMPRESS_PRESET = os.environ.get("COMPRESS_PRESET", "veryfast")
CATEGORY = "https://www.footballorgin.com/international-games/fifa-world-cup-2026/"
MAX_CATEGORY_PAGES = 12
VARIANT_RETRIES = 3

_log_lock = threading.Lock()
_progress_lock = threading.Lock()

# Team-name normalisation: the fixture list and the site URLs disagree on a few
# names. Map the fixture-list spelling to the site spelling.
TEAM_ALIAS = {
    "korea republic": "south korea",
    "united states": "usa",
    "curacao": "curacao",
}

START_TS = time.time()


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}"
    with _log_lock:
        print(line, flush=True)
        os.makedirs(LOGDIR, exist_ok=True)
        with open(LOGFILE, "a") as fh:
            fh.write(line + "\n")


def strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def canon(team: str) -> str:
    """Normalise a team name to a comparable slug (alias + drop 'and')."""
    s = strip_accents(team).lower().strip()
    s = TEAM_ALIAS.get(s, s)
    s = re.sub(r"\band\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return re.sub(r"\s+", "-", s)


def split_teams_from_url(url: str):
    """Return (left, right) canonical team slugs parsed from a post URL."""
    seg = url.rstrip("/").rsplit("/", 1)[-1]
    seg = re.split(r"-full-match", seg)[0]
    for sep in ("-vs-", "-v-"):
        if sep in seg:
            left, right = seg.split(sep, 1)
            # canon by turning dashes back into spaces (drops 'and', accents)
            return canon(left.replace("-", " ")), canon(right.replace("-", " "))
    return None


def build_url_index():
    ua = fc.USER_AGENT
    seen = []
    for p in range(1, MAX_CATEGORY_PAGES + 1):
        url = CATEGORY if p == 1 else CATEGORY + f"page/{p}/"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            html = urllib.request.urlopen(req, timeout=45).read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            log(f"  category page {p} stopped: {exc}")
            break
        links = re.findall(
            r"https://www\.footballorgin\.com/[a-z0-9-]+-full-match-[0-9a-z-]+/", html
        )
        new = [l for l in dict.fromkeys(links) if l not in seen]
        seen.extend(new)
        if not new:
            break
        time.sleep(0.5)

    ordered: dict[tuple, str] = {}
    by_set: dict[frozenset, list] = {}
    for url in seen:
        teams = split_teams_from_url(url)
        if not teams:
            continue
        ordered.setdefault(teams, url)
        by_set.setdefault(frozenset(teams), []).append(url)
    log(f"Built URL index: {len(seen)} posts, {len(ordered)} ordered keys")
    return ordered, by_set


def resolve_match_url(name: str, ordered, by_set):
    parts = [p for p in re.split(r"\s+v\s+", name.strip())]
    if len(parts) != 2:
        return None
    key = (canon(parts[0]), canon(parts[1]))
    if key in ordered:
        return ordered[key]
    cand = by_set.get(frozenset(key))
    if cand and len(cand) == 1:
        return cand[0]
    if cand:
        # Ambiguous (teams meet twice); pick the one whose left side matches.
        for u in cand:
            t = split_teams_from_url(u)
            if t and t[0] == key[0]:
                return u
        return cand[0]
    return None


def slugify_match(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", strip_accents(name).lower()).strip("_")


def variant_stem(label: str) -> str:
    return fc.slugify(label).replace("-", "_") or "video"


def ffprobe_json(path: str) -> dict:
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_format", "-show_streams",
                "-of", "json", path,
            ],
            capture_output=True, text=True, timeout=120,
        )
        return json.loads(out.stdout or "{}")
    except Exception:  # noqa: BLE001
        return {}


def probe_ok(path: str, min_seconds: float = 1.0):
    """Return (ok, duration, video_bitrate) for a media file."""
    if not os.path.exists(path) or os.path.getsize(path) < 10000:
        return False, 0.0, 0
    info = ffprobe_json(path)
    fmt = info.get("format", {})
    streams = info.get("streams", [])
    has_video = any(s.get("codec_type") == "video" for s in streams)
    try:
        dur = float(fmt.get("duration", 0) or 0)
    except (TypeError, ValueError):
        dur = 0.0
    vbit = 0
    for s in streams:
        if s.get("codec_type") == "video":
            try:
                vbit = int(s.get("bit_rate") or 0)
            except (TypeError, ValueError):
                vbit = 0
            break
    if not vbit:
        try:
            vbit = int(fmt.get("bit_rate") or 0)
        except (TypeError, ValueError):
            vbit = 0
    return (has_video and dur >= min_seconds), dur, vbit


def compress(src: str, dst: str, target_bitrate: int) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tb = max(target_bitrate, 150_000)  # floor so 480p never looks terrible
    cmd = [
        "ffmpeg", "-y", "-i", src,
        "-vf", "scale=-2:480,fps=30",
        "-c:v", "libx265", "-tag:v", "hvc1",
        "-b:v", str(tb), "-maxrate", str(int(tb * 1.5)), "-bufsize", str(int(tb * 2)),
        "-preset", COMPRESS_PRESET,
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        dst,
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def load_progress() -> dict:
    if os.path.exists(PROGRESS_JSON):
        try:
            with open(PROGRESS_JSON) as fh:
                data = json.load(fh)
        except Exception:  # noqa: BLE001
            data = {}
    else:
        data = {}
    data.setdefault("completed", [])
    data.setdefault("failed", [])
    data.setdefault("skipped", [])
    data.setdefault("details", {})
    return data


def save_progress(data: dict) -> None:
    txt = json.dumps(data, indent=2, ensure_ascii=False)
    with open(PROGRESS_JSON, "w") as fh:
        fh.write(txt)
    os.makedirs(PROGRESSDIR, exist_ok=True)
    with open(os.path.join(PROGRESSDIR, "progress.json"), "w") as fh:
        fh.write(txt)


def human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f}{unit}"
        f /= 1024


def process_match(name: str, url: str):
    """Download + compress every variant.

    Returns (status, detail) where status is 'success'|'failed'|'skipped'.
    Does not touch shared state -- the caller records the result.
    """
    slug = slugify_match(name)
    outdir = os.path.join(DOWNLOADS, slug)
    compdir = os.path.join(outdir, "compressed")
    os.makedirs(compdir, exist_ok=True)

    log(f"  {slug}: resolving variants for {url}")
    try:
        post_html = fc.http_get(url)
        variants = fc.parse_variants(post_html)
    except Exception as exc:  # noqa: BLE001
        log(f"  ERROR fetching post page: {exc}")
        variants = []

    if not variants:
        log(f"  {name}: no variants found -> skipped")
        return "skipped", {"match": name, "url": url, "status": "skipped", "errors": ["no variants"]}

    detail = {
        "match": name,
        "url": url,
        "variants": {},
        "originals": [],
        "compressed": [],
        "errors": [],
        "status": "failed",
    }

    any_ok = False
    for v in variants:
        stem = variant_stem(v.label)
        orig = os.path.join(outdir, f"{stem}.mp4")
        comp = os.path.join(compdir, f"{stem}_480p30_h265.mp4")

        # --- download original (with retries) ---
        ok, dur, vbit = probe_ok(orig)
        if not ok:
            for attempt in range(1, VARIANT_RETRIES + 1):
                try:
                    log(f"  {slug} [{v.index}] {v.label}: downloading original (try {attempt})")
                    _, res = fc.resolve(url, None, v.index)
                    fc.download_hls(res.m3u8_url, orig, referer=res.player_url, height=None)
                    ok, dur, vbit = probe_ok(orig)
                    if ok:
                        break
                    log(f"  {slug} [{v.index}] {v.label}: probe failed after download")
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc)
                    # Some variants (typically "Full match") are not embedded
                    # server-side; that's permanent -> don't waste retries.
                    if "iframe" in msg.lower() or "player page" in msg.lower():
                        log(f"  {slug} [{v.index}] {v.label}: not embedded ({msg}) -> skip variant")
                        break
                    log(f"  {slug} [{v.index}] {v.label}: download error: {msg}")
                    time.sleep(min(2 ** attempt, 15))
        if not ok:
            detail["errors"].append(f"{v.label}: original unavailable/failed")
            continue
        log(f"  {slug} [{v.index}] {v.label}: original OK {human(os.path.getsize(orig))} {dur:.0f}s {human(vbit)}/s")
        detail["originals"].append(os.path.relpath(orig, ROOT))

        # --- compress (with retries) ---
        cok, cdur, _ = probe_ok(comp)
        if not cok:
            target = int(vbit * 0.6) if vbit else 0
            for attempt in range(1, VARIANT_RETRIES + 1):
                try:
                    log(f"  {slug} [{v.index}] {v.label}: compressing -> 480p30 ~{human(target)}/s (try {attempt})")
                    compress(orig, comp, target)
                    cok, cdur, _ = probe_ok(comp)
                    if cok:
                        break
                except subprocess.CalledProcessError as exc:
                    log(f"  {slug} [{v.index}] {v.label}: ffmpeg error: {exc.stderr[-400:] if exc.stderr else exc}")
                    time.sleep(2)
                except Exception as exc:  # noqa: BLE001
                    log(f"  {slug} [{v.index}] {v.label}: compress error: {exc}")
                    time.sleep(2)
        if not cok:
            detail["errors"].append(f"{v.label}: compression failed")
            continue
        log(f"  {slug} [{v.index}] {v.label}: compressed OK {human(os.path.getsize(comp))} {cdur:.0f}s")
        detail["compressed"].append(os.path.relpath(comp, ROOT))
        detail["variants"][v.label] = {
            "original": os.path.relpath(orig, ROOT),
            "compressed": os.path.relpath(comp, ROOT),
        }
        any_ok = True

    detail["status"] = "success" if any_ok else "failed"
    return detail["status"], detail


def main() -> int:
    for d in (DOWNLOADS, LOGDIR, REPORTDIR, PROGRESSDIR):
        os.makedirs(d, exist_ok=True)
    log(f"=== run start: target {TARGET_SUCCESSES} new successful matches ===")

    with open(MATCHES_TXT) as fh:
        matches = [ln.strip() for ln in fh if ln.strip()]

    progress = load_progress()
    completed = set(progress["completed"])
    log(f"Already completed: {sorted(completed)}")

    ordered, by_set = build_url_index()

    # Build the ordered candidate queue (available, not-yet-completed matches).
    candidates = []
    for name in matches:
        if name in completed:
            continue
        url = resolve_match_url(name, ordered, by_set)
        if not url:
            log(f"UNAVAILABLE (no post URL): {name} -> skipped")
            with _progress_lock:
                for lst in ("completed", "failed", "skipped"):
                    if name in progress[lst]:
                        progress[lst].remove(name)
                progress["skipped"].append(name)
                save_progress(progress)
            continue
        candidates.append((name, url))

    log(f"{len(candidates)} candidate matches; need {TARGET_SUCCESSES} successes; {MAX_WORKERS} workers")

    def record(name: str, status: str, detail: dict) -> None:
        with _progress_lock:
            progress["details"][name] = detail
            for lst in ("completed", "failed", "skipped"):
                if name in progress[lst]:
                    progress[lst].remove(name)
            progress[status_to_list(status)].append(name)
            save_progress(progress)

    import shutil

    def free_gb() -> float:
        return shutil.disk_usage(ROOT).free / 1e9

    new_success = 0
    ci = 0
    stop_disk = False
    inflight: dict = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        while new_success < TARGET_SUCCESSES and (inflight or ci < len(candidates)):
            if not stop_disk and free_gb() < MIN_FREE_GB:
                stop_disk = True
                log(f"!! low disk ({free_gb():.0f}GB < {MIN_FREE_GB}GB) -> stop starting new matches")
            while (
                not stop_disk
                and ci < len(candidates)
                and len(inflight) < MAX_WORKERS
                and (new_success + len(inflight)) < TARGET_SUCCESSES
            ):
                name, url = candidates[ci]
                ci += 1
                log(f"--- START: {name}")
                inflight[ex.submit(process_match, name, url)] = name
            if not inflight:
                break
            done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
            for fut in done:
                name = inflight.pop(fut)
                try:
                    status, detail = fut.result()
                except Exception as exc:  # noqa: BLE001
                    log(f"  {name}: unexpected error: {exc}")
                    status, detail = "failed", {"match": name, "status": "failed", "errors": [str(exc)]}
                record(name, status, detail)
                if status == "success":
                    new_success += 1
                    log(f"  => SUCCESS: {name} ({new_success}/{TARGET_SUCCESSES})")
                elif status == "skipped":
                    log(f"  => SKIPPED: {name}")
                else:
                    log(f"  => FAILED: {name} (continuing)")

    with _progress_lock:
        write_report(progress, new_success)
    log(f"=== run done: {new_success} new successful matches ===")
    return 0


def status_to_list(status: str) -> str:
    return {"success": "completed", "skipped": "skipped"}.get(status, "failed")


def write_report(progress: dict, new_success: int) -> None:
    runtime = time.time() - START_TS
    total_bytes = 0
    for dirpath, _dirs, files in os.walk(DOWNLOADS):
        for f in files:
            if f.endswith(".mp4"):
                try:
                    total_bytes += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass

    lines = []
    lines.append("# FIFA World Cup 2026 — Download Report")
    lines.append("")
    lines.append(f"_Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}_")
    lines.append("")
    lines.append(f"- New successful matches this run: **{new_success}**")
    lines.append(f"- Total completed: **{len(progress['completed'])}**")
    lines.append(f"- Failed: **{len(progress['failed'])}**")
    lines.append(f"- Skipped/unavailable: **{len(progress['skipped'])}**")
    lines.append(f"- Total downloaded size (mp4): **{human(total_bytes)}**")
    lines.append(f"- Total runtime: **{runtime/60:.1f} min**")
    lines.append("")

    lines.append("## Successful downloads")
    lines.append("")
    for name in progress["completed"]:
        d = progress["details"].get(name)
        if not d:
            lines.append(f"- {name}")
            continue
        lines.append(f"### {name}")
        lines.append(f"- source: {d.get('url','?')}")
        for label, paths in d.get("variants", {}).items():
            lines.append(
                f"  - {label}: `{paths['original']}` -> `{paths['compressed']}`"
            )
        lines.append("")

    if progress["failed"]:
        lines.append("## Failures")
        lines.append("")
        for name in progress["failed"]:
            d = progress["details"].get(name, {})
            errs = "; ".join(d.get("errors", [])) or "unknown"
            lines.append(f"- {name}: {errs}")
        lines.append("")

    if progress["skipped"]:
        lines.append("## Skipped / unavailable")
        lines.append("")
        for name in progress["skipped"]:
            lines.append(f"- {name}")
        lines.append("")

    os.makedirs(REPORTDIR, exist_ok=True)
    with open(os.path.join(REPORTDIR, "report.md"), "w") as fh:
        fh.write("\n".join(lines))
    log(f"Wrote {os.path.join(REPORTDIR, 'report.md')}")


if __name__ == "__main__":
    raise SystemExit(main())
