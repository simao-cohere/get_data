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
import soccerfull_crawler as sf

ROOT = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS = os.environ.get(
    "DOWNLOADS",
    "/data/1d/simao/football_downloads/downloads",
)
LOGDIR = os.path.join(ROOT, "logs")
REPORTDIR = os.path.join(ROOT, "reports")
PROGRESSDIR = os.path.join(ROOT, "progress")
PROGRESS_JSON = os.path.join(ROOT, "progress.json")
MATCHES_TXT = os.environ.get("MATCHES_TXT", os.path.join(ROOT, "world_cup_matches.txt"))
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

# Fallback for matches whose post exists on the site but isn't reachable via
# the paginated category listing (e.g. not yet indexed there). Checked before
# the scraped URL index.
MANUAL_URLS = {
    "norway v england": "https://www.footballorgin.com/norway-v-england-full-match-11-july-2026/",
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
    """Convert a fixture name to a filesystem slug, applying team aliases."""
    parts = re.split(r"\s+v\s+", strip_accents(name).lower(), maxsplit=1)
    if len(parts) == 2:
        parts = [TEAM_ALIAS.get(p.strip(), p.strip()) for p in parts]
        normalised = " v ".join(parts)
    else:
        normalised = strip_accents(name).lower()
    return re.sub(r"[^a-z0-9]+", "_", normalised).strip("_")


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


def classify_variant(label: str) -> str:
    """Bucket a variant label into a canonical kind."""
    l = label.lower()
    if "extra" in l or "penalt" in l:
        return "extra_time"
    if "1st" in l or "first" in l:
        return "first_half"
    if "2nd" in l or "second" in l:
        return "second_half"
    if "highl" in l:  # matches "Highlights" and the site's "Highllights" typo
        return "highlights"
    if "full" in l:
        return "full_match"
    return "other"


def choose_variants(variants):
    """Pick which variants to actually download.

    Policy (per user preference): separate halves beat the full match. When
    both a 1st and a 2nd half are available, the 'Full match' variant is
    dropped -- the halves (plus any extra time and highlights) cover the whole
    game and are what we want. The full match is kept only when there is no
    pair of halves. Variants that collapse to the same kind (e.g. duplicate
    'Full Match' entries, or 'Highlights'/'Highllights') are de-duplicated.
    """
    kinds = {classify_variant(v.label) for v in variants}
    has_halves = "first_half" in kinds and "second_half" in kinds
    chosen = []
    seen = set()
    for v in variants:
        k = classify_variant(v.label)
        if has_halves and k == "full_match":
            continue
        key = f"other:{variant_stem(v.label)}" if k == "other" else k
        if key in seen:
            continue
        seen.add(key)
        chosen.append(v)
    return chosen


def _fetch_variants(slug, outdir, compdir, variants, resolver, detail):
    """Download + compress ``variants`` using ``resolver``.

    ``resolver(variant)`` returns ``(m3u8_url, referer)`` or raises. Files are
    keyed by variant stem, so a variant already downloaded by a previous source
    (e.g. footballorgin before falling back to soccerfull) is detected via
    ``probe_ok`` and not fetched again. Returns the set of variant kinds that
    are available (downloaded + compressed OK).
    """
    got = set()
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
                    m3u8_url, referer = resolver(v)
                    fc.download_hls(m3u8_url, orig, referer=referer, height=None)
                    ok, dur, vbit = probe_ok(orig)
                    if ok:
                        break
                    log(f"  {slug} [{v.index}] {v.label}: probe failed after download")
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc)
                    # A missing iframe / player / m3u8 is permanent for this
                    # source -> don't waste retries (the other source may work).
                    if any(s in msg.lower() for s in ("iframe", "player page", ".m3u8")):
                        log(f"  {slug} [{v.index}] {v.label}: unresolvable ({msg}) -> skip variant")
                        break
                    log(f"  {slug} [{v.index}] {v.label}: download error: {msg}")
                    time.sleep(min(2 ** attempt, 15))
        if not ok:
            detail["errors"].append(f"{v.label}: original unavailable/failed")
            continue
        log(f"  {slug} [{v.index}] {v.label}: original OK {human(os.path.getsize(orig))} {dur:.0f}s {human(vbit)}/s")
        rel_orig = os.path.relpath(orig, ROOT)
        if rel_orig not in detail["originals"]:
            detail["originals"].append(rel_orig)

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
        got.add(classify_variant(v.label))
    return got


def process_match(name: str, fo_url: str | None, sf_url: str | None = None):
    """Download + compress a match, preferring footballorgin then soccerfull.

    footballorgin.com is tried first; soccerfull.net (the origin host that
    footballorgin embeds) is used as a fallback whenever we don't already have
    both halves -- which recovers the matches whose footballorgin player iframe
    is JS-injected (and therefore invisible to the scraper). Variant selection
    prefers separate halves over the full match (see ``choose_variants``).

    Returns (status, detail) where status is 'success'|'failed'|'skipped'.
    Does not touch shared state -- the caller records the result.
    """
    slug = slugify_match(name)
    outdir = os.path.join(DOWNLOADS, slug)
    compdir = os.path.join(outdir, "compressed")
    os.makedirs(compdir, exist_ok=True)

    detail = {
        "match": name,
        "url": fo_url,
        "soccerfull_url": sf_url,
        "variants": {},
        "originals": [],
        "compressed": [],
        "errors": [],
        "status": "failed",
    }

    got: set[str] = set()

    # --- primary source: footballorgin.com ---
    if fo_url:
        log(f"  {slug}: resolving footballorgin variants for {fo_url}")
        try:
            post_html = fc.http_get(fo_url)
            fo_variants = fc.parse_variants(post_html)
        except Exception as exc:  # noqa: BLE001
            log(f"  {slug}: ERROR fetching footballorgin post page: {exc}")
            fo_variants = []
        if fo_variants:
            def fo_resolver(v):
                _, res = fc.resolve(fo_url, None, v.index)
                return res.m3u8_url, (res.referer or res.player_url)

            got |= _fetch_variants(
                slug, outdir, compdir, choose_variants(fo_variants), fo_resolver, detail
            )
        else:
            log(f"  {slug}: no footballorgin variants found")

    # --- fallback / complement: soccerfull.net (the origin host) ---
    have_halves = "first_half" in got and "second_half" in got
    if sf_url and not have_halves:
        log(f"  {slug}: soccerfull fallback {sf_url}")
        try:
            sf_variants = sf.resolve(sf_url)
        except Exception as exc:  # noqa: BLE001
            log(f"  {slug}: soccerfull fetch error: {exc}")
            sf_variants = []
        if sf_variants:
            got |= _fetch_variants(
                slug, outdir, compdir, choose_variants(sf_variants), sf.resolve_variant, detail
            )

    if got:
        detail["status"] = "success"
    elif not (fo_url or sf_url):
        detail["status"] = "skipped"
        detail.setdefault("errors", []).append("no post URL on either site")
    else:
        detail["status"] = "failed"
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

    # soccerfull.net is the origin host footballorgin embeds; use it as a
    # fallback for matches footballorgin can't resolve. Failure to build the
    # index just disables the fallback -- the run still proceeds.
    try:
        sf_ordered, sf_by_set = sf.build_url_index(log_fn=log)
    except Exception as exc:  # noqa: BLE001
        log(f"soccerfull index unavailable ({exc}); fallback disabled")
        sf_ordered, sf_by_set = {}, {}

    # Build the ordered candidate queue (available, not-yet-completed matches).
    candidates = []
    for name in matches:
        if name in completed:
            continue
        fo_url = MANUAL_URLS.get(name) or resolve_match_url(name, ordered, by_set)
        sf_url = sf.find_match_url(name, sf_ordered, sf_by_set) if sf_ordered else None
        if not fo_url and not sf_url:
            log(f"UNAVAILABLE (no post URL on either site): {name} -> skipped")
            with _progress_lock:
                for lst in ("completed", "failed", "skipped"):
                    if name in progress[lst]:
                        progress[lst].remove(name)
                progress["skipped"].append(name)
                save_progress(progress)
            continue
        candidates.append((name, fo_url, sf_url))

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
                name, fo_url, sf_url = candidates[ci]
                ci += 1
                log(f"--- START: {name}")
                inflight[ex.submit(process_match, name, fo_url, sf_url)] = name
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
        lines.append(f"- source: {d.get('url') or d.get('soccerfull_url') or '?'}")
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
