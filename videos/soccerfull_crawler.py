#!/usr/bin/env python3
"""Crawler for soccerfull.net match videos — a complement/fallback to footballorgin.

soccerfull.net is the *origin* host that footballorgin.com embeds. Resolving it
directly avoids footballorgin's JS-injected player iframe, which is the single
biggest source of download failures (`Could not find a player iframe ...`). The
page chain here is short and fully server-rendered (no JS needed):

    1. Category listing
           https://soccerfull.net/cate/world-cup-2026[/<page>]
       lists matches as
           <a href="/<slug>-<postid>.html" title="Team A vs Team B"> ...

    2. Match page
           https://soccerfull.net/<slug>-<postid>.html[?sid=<id>]
       carries one variant tab per stream:
           <a href="?sid=<id>">1st Half | 2nd Half | Full Match | Highlight</a>
       and an <iframe src="/play/<sid>"> for the selected one. Each ``sid`` *is*
       the soccerfull play id.

    3. Player page
           https://soccerfull.net/play/<sid>
       runs ArtPlayer with an HLS source — the exact same player the
       footballorgin chain lands on, so we reuse
       ``footballorgin_crawler.extract_m3u8`` / ``download_hls`` unchanged.

Usage:

    # List the variants for a match page
    python3 soccerfull_crawler.py "https://soccerfull.net/argentina-vs-egypt-3535.html" --list

    # Find a match by fixture name (scrapes the WC-2026 category listing)
    python3 soccerfull_crawler.py --find "argentina v egypt" --list

    # Download a variant (by label or by sid)
    python3 soccerfull_crawler.py --find "argentina v egypt" --variant "1st half" -o out.mp4
    python3 soccerfull_crawler.py "https://soccerfull.net/argentina-vs-egypt-3535.html" --sid 14636
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import unicodedata

import footballorgin_crawler as fc

BASE = "https://soccerfull.net"
CATEGORY = f"{BASE}/cate/world-cup-2026"
MAX_CATEGORY_PAGES = 12

# See footballorgin_crawler.DEFAULT_OUT_DIR: manual CLI downloads default to
# the large data volume, not a relative "downloads/" folder under the repo.
DEFAULT_OUT_DIR = os.environ.get(
    "DOWNLOADS_OUT_DIR", "/data/1d/simao/football_downloads/manual_downloads"
)

# Reuse the footballorgin Variant dataclass (index/label/page_url). For soccerfull
# ``index`` holds the sid (== play id) and ``page_url`` is the /play/<sid> URL.
Variant = fc.Variant

# soccerfull spells a few teams differently from the fixture list.
TEAM_ALIAS = {
    "korea republic": "south korea",
    "united states": "usa",
}


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def canon(team: str) -> str:
    """Normalise a team name to a comparable slug (drop accents / 'and')."""
    s = _strip_accents(team).lower().strip()
    s = re.sub(r"\band\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return re.sub(r"\s+", "-", s)


def _canon_candidates(team: str) -> set[str]:
    """All canonical spellings to try for one team (raw + aliases)."""
    base = _strip_accents(team).lower().strip()
    out = {canon(team)}
    if base in TEAM_ALIAS:
        out.add(canon(TEAM_ALIAS[base]))
    for fixture_name, site_name in TEAM_ALIAS.items():
        if base == fixture_name or canon(team) == canon(site_name):
            out.add(canon(fixture_name))
            out.add(canon(site_name))
    return out


def _teams_from_title(title: str):
    parts = re.split(r"\s+v(?:s)?\s+", title.strip(), maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2:
        return None
    return canon(parts[0]), canon(parts[1])


def _teams_from_slug(href: str):
    seg = href.strip("/").rsplit(".html", 1)[0]
    seg = re.sub(r"-\d+$", "", seg)  # drop the trailing -<postid>
    for sep in ("-vs-", "-v-"):
        if sep in seg:
            left, right = seg.split(sep, 1)
            return canon(left.replace("-", " ")), canon(right.replace("-", " "))
    return None


_MATCH_LINK_RE = re.compile(
    r'<a\s+href="(/[a-z0-9-]+-\d+\.html)"\s+title="([^"]+)"', re.IGNORECASE
)


def build_url_index(log_fn=None):
    """Scrape the WC-2026 category listing into a name -> match-URL index.

    Returns ``(ordered, by_set)`` where ``ordered`` maps ``(left, right)``
    canonical team slugs to a URL and ``by_set`` maps an unordered team pair to
    the list of matching URLs (for order-insensitive / rematch lookups).
    """
    ordered: dict[tuple, str] = {}
    by_set: dict[frozenset, list] = {}
    seen_urls: set[str] = set()

    for p in range(1, MAX_CATEGORY_PAGES + 1):
        url = CATEGORY if p == 1 else f"{CATEGORY}/{p}"
        try:
            html = fc.http_get(url)
        except Exception as exc:  # noqa: BLE001
            if log_fn:
                log_fn(f"  soccerfull category page {p} stopped: {exc}")
            break
        new = 0
        for href, title in _MATCH_LINK_RE.findall(html):
            full = BASE + href
            if full in seen_urls:
                continue
            seen_urls.add(full)
            teams = _teams_from_title(title) or _teams_from_slug(href)
            if not teams:
                continue
            ordered.setdefault(teams, full)
            by_set.setdefault(frozenset(teams), []).append(full)
            new += 1
        if new == 0:
            break
        time.sleep(0.3)

    if log_fn:
        log_fn(f"soccerfull index: {len(seen_urls)} posts, {len(ordered)} ordered keys")
    return ordered, by_set


def find_match_url(name: str, ordered, by_set):
    """Resolve a fixture name (e.g. 'argentina v egypt') to a match URL."""
    parts = re.split(r"\s+v\s+", name.strip())
    if len(parts) != 2:
        return None
    a_cands = _canon_candidates(parts[0])
    b_cands = _canon_candidates(parts[1])
    for a in a_cands:
        for b in b_cands:
            if (a, b) in ordered:
                return ordered[(a, b)]
            if (b, a) in ordered:
                return ordered[(b, a)]
    for a in a_cands:
        for b in b_cands:
            cand = by_set.get(frozenset((a, b)))
            if cand:
                return cand[0]
    return None


_TAB_RE = re.compile(
    r'<a\s+href="\?sid=(\d+)"[^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE
)


def parse_match_variants(match_html: str) -> list[Variant]:
    """Parse the variant tabs (1st half / 2nd half / Full match / Highlight)."""
    variants: list[Variant] = []
    seen: set[str] = set()
    for sid, inner in _TAB_RE.findall(match_html):
        if sid in seen:
            continue
        label = re.sub(r"<[^>]+>", " ", inner)
        label = re.sub(r"\s+", " ", label).strip()
        if not label:
            continue
        seen.add(sid)
        variants.append(
            Variant(index=int(sid), label=label, page_url=f"{BASE}/play/{sid}")
        )
    return variants


def resolve_variant(variant: Variant) -> tuple[str, str]:
    """Return ``(m3u8_url, referer)`` for a soccerfull variant."""
    player_url = variant.page_url
    player_html = fc.http_get(player_url, referer=BASE + "/")
    m3u8_url = fc.extract_m3u8(player_html, player_url)
    return m3u8_url, player_url


def resolve(match_url: str):
    """Fetch a match page and return its list of variants."""
    html = fc.http_get(match_url)
    variants = parse_match_variants(html)
    if not variants:
        raise RuntimeError("No variant tabs found on the soccerfull match page.")
    return variants


def _select(variants: list[Variant], variant: str | None, sid: int | None) -> Variant:
    if sid is not None:
        for v in variants:
            if v.index == sid:
                return v
        raise RuntimeError(f"No variant with sid {sid}.")
    if variant is not None:
        needle = variant.strip().lower()
        for v in variants:
            if v.label.lower() == needle:
                return v
        for v in variants:
            if needle in v.label.lower():
                return v
        raise RuntimeError(f"No variant matching label {variant!r}.")
    for v in variants:
        if "highl" in v.label.lower():
            return v
    return variants[0]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("match_url", nargs="?", help="soccerfull.net match page URL.")
    parser.add_argument("--find", help="Resolve a match page by fixture name (e.g. 'argentina v egypt').")
    parser.add_argument("--variant", help="Variant label to download (substring match allowed).")
    parser.add_argument("--sid", type=int, help="Variant sid (play id) to download.")
    parser.add_argument("--list", action="store_true", help="List available variants and exit.")
    parser.add_argument("--resolve-only", action="store_true", help="Print the .m3u8 URL without downloading.")
    parser.add_argument("-o", "--output", help="Output .mp4 path.")
    parser.add_argument(
        "--out-dir",
        default=DEFAULT_OUT_DIR,
        help=f"Directory for downloads (default: {DEFAULT_OUT_DIR}; override with DOWNLOADS_OUT_DIR env var).",
    )
    parser.add_argument("--height", type=int, help="Downscale to this video height in one re-encode pass.")
    parser.add_argument("--crf", type=int, default=23, help="x264 CRF quality when --height is used (default 23).")
    args = parser.parse_args()

    match_url = args.match_url
    if not match_url and args.find:
        ordered, by_set = build_url_index(log_fn=lambda m: print(m, file=sys.stderr))
        match_url = find_match_url(args.find, ordered, by_set)
        if not match_url:
            print(f"No soccerfull match found for {args.find!r}.", file=sys.stderr)
            return 1
        print(f"Resolved: {match_url}", file=sys.stderr)
    if not match_url:
        parser.error("Provide a match URL or use --find <fixture name>.")

    try:
        variants = resolve(match_url)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.list:
        print(f"Variants for {match_url}:")
        for v in variants:
            print(f"  [sid={v.index}] {v.label}  -> {v.page_url}")
        return 0

    chosen = _select(variants, args.variant, args.sid)
    m3u8_url, referer = resolve_variant(chosen)
    print(f"Selected:  [sid={chosen.index}] {chosen.label}", file=sys.stderr)
    print(f"Player:    {chosen.page_url}", file=sys.stderr)
    print(f"M3U8:      {m3u8_url}", file=sys.stderr)
    if args.resolve_only:
        print(m3u8_url)
        return 0

    out_path = args.output or os.path.join(args.out_dir, f"{fc.slugify(chosen.label)}.mp4")
    try:
        fc.download_hls(m3u8_url, out_path, referer=referer, height=args.height, crf=args.crf)
    except Exception as exc:  # noqa: BLE001
        print(f"Download failed: {exc}", file=sys.stderr)
        return 1
    print(f"\nSaved: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
