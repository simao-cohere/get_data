#!/usr/bin/env python3
"""Lean, match-scoped SofaScore metadata crawler.

Fetches only the data that is useful for a video-retrieval / multimodal QA
system over match footage, then aggregates it into a single ``metadata.json``.

SofaScore's public API (``api.sofascore.com/api/v1``) sits behind a Varnish
edge that returns ``403 {"reason":"challenge"}`` to plain clients. Two things
are required to get ``200`` responses:

  1. a real browser TLS/JA3 fingerprint  -> provided by ``curl_cffi`` impersonate
  2. the header ``X-Requested-With: XMLHttpRequest``  (the actual gatekeeper)

What is fetched (and why):

  Time-resolved (high value) : event, incidents, shotmap, comments, highlights,
                               graph (momentum)
  Lookups / aggregates       : statistics, lineups, best-players, per-player
                               event statistics, group standings (total)
  Media                      : player / team / manager photos, tournament logo

What is deliberately NOT fetched (low value for video QA): heatmaps, player
career profiles, average-positions, h2h, pregame-form, team-streaks, odds,
predictions/votes, team recent/upcoming/performance/season stats, referee /
venue / tournament profile JSON (all already embedded in the event object).

Output layout. The base dir defaults to ``data/`` (git-ignored):

    data/<home_v_away>/
        match.json         aggregated, agent-ready (written by aggregate.py)
        event.json         raw SofaScore event core (pruned after aggregation)
        lineups.json       full lineup + per-player stats + kit colours
        shots.json         granular shotmap with coordinates and xG
        comments.json      minute-by-minute commentary feed
        statistics.json    team aggregates (all / 1st / 2nd half)
        best_players.json  match ratings summary
        ...                other raw endpoints (pruned after aggregation)
    images/            shared across all matches — lives next to data/
        unique_tournament.png
        portugal/
            team_<id>.png
            manager_<id>.png
            player_<id>.png  ← Ronaldo stored once, not once per match
        uzbekistan/
            ...

Images are skipped on re-crawl if they already exist on disk.

Usage:
    python crawl.py [--event-id 15186858] [--out data] [--delay 0.4]
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone

from curl_cffi import requests

API = "https://api.sofascore.com/api/v1"
WEB = "https://www.sofascore.com"
IMPERSONATE = "chrome120"

# Normalise SofaScore team slugs to the shared vocabulary used by both crawlers.
# Applied after replacing hyphens with underscores in the raw API slug.
SLUG_NORMALISE = {
    "cote_d_ivoire": "ivory_coast",
    "cote_divoire":  "ivory_coast",   # alternate SofaScore spelling
}

# The header that actually unlocks the Varnish edge, plus polite browser-like
# context headers.
BASE_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{WEB}/",
    "Origin": WEB,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Response headers we keep in metadata (never store cookies / auth).
_DROP_HEADER_PREFIXES = ("set-cookie", "cookie", "authorization", "cf-")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def strip_field_translations(obj):
    """Recursively remove ``fieldTranslations`` keys (per-language name dicts).

    Keeps the English ``name``/``shortName`` fields and any unicode that appears
    in actual foreign names; only the bulky translation dictionaries are dropped.
    """
    if isinstance(obj, dict):
        obj.pop("fieldTranslations", None)
        for v in obj.values():
            strip_field_translations(v)
    elif isinstance(obj, list):
        for v in obj:
            strip_field_translations(v)
    return obj


class Crawler:
    def __init__(self, event_id: int, out_base: str = ".", delay: float = 0.4,
                 prune_sources: bool = False):
        self.event_id = event_id
        self.out_base = os.path.abspath(out_base)
        self.delay = delay
        self.prune_sources = prune_sources
        self.session = requests.Session(impersonate=IMPERSONATE)
        self.session.headers.update(BASE_HEADERS)
        self.visited: set[str] = set()
        # set once the slug is known (after the event core is fetched)
        self.out: str | None = None
        self.media_dir: str | None = None
        # discovered ids (filled as we crawl)
        self.player_ids: set[int] = set()
        self.team_ids: set[int] = set()
        self.manager_ids: set[int] = set()
        self.unique_tournament_id: int | None = None
        self.season_id: int | None = None
        # id → country_slug mappings (built from event core + lineups)
        self.player_country: dict[int, str] = {}
        self.team_country: dict[int, str] = {}
        self.manager_country: dict[int, str] = {}

    def _setup(self, slug: str) -> None:
        self.out = os.path.join(self.out_base, slug)
        # Shared images dir sits next to data/, not inside the match dir.
        # e.g.  metadata/images/  rather than  metadata/data/<match>/images/
        self.media_dir = os.path.join(os.path.dirname(self.out_base), "images")
        for d in (self.out, self.media_dir):
            os.makedirs(d, exist_ok=True)

    # ----------------------------------------------------------------- fetch
    def fetch(self, url: str, binary: bool = False, retries: int = 4):
        """GET a url with retries. Returns (meta_dict, content) or (meta, None)."""
        if url in self.visited:
            return None, None
        self.visited.add(url)
        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                r = self.session.get(url, timeout=40)
                headers = {
                    k: v for k, v in r.headers.items()
                    if not k.lower().startswith(_DROP_HEADER_PREFIXES)
                }
                meta = {
                    "url": url,
                    "method": "GET",
                    "status": r.status_code,
                    "content_type": r.headers.get("content-type", ""),
                    "size": len(r.content),
                    "retrieved_at": now_iso(),
                    "headers": headers,
                }
                content = r.content if binary else r.text
                if r.status_code in (403, 429, 500, 502, 503) and attempt < retries:
                    time.sleep(min(2 ** attempt, 12))
                    last_exc = f"status {r.status_code}"
                    continue
                time.sleep(self.delay)
                return meta, content
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < retries:
                    time.sleep(min(2 ** attempt, 12))
        meta = {
            "url": url, "method": "GET", "status": -1, "content_type": "",
            "size": 0, "retrieved_at": now_iso(), "headers": {},
            "error": str(last_exc),
        }
        return meta, None

    def _write_source(self, name: str, obj) -> None:
        path = os.path.join(self.out, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))

    def get_json(self, url: str, name: str):
        """Fetch a JSON endpoint, strip translations, store under sources/."""
        meta, content = self.fetch(url)
        if meta is None:
            return None
        if meta["status"] != 200 or content is None:
            print(f"  [{meta['status']}] {url}")
            return None
        try:
            obj = json.loads(content)
        except Exception:
            print(f"  [parse-fail] {url}")
            return None
        strip_field_translations(obj)
        self._write_source(name, obj)
        print(f"  [200] {meta['size']:>8d}  {url}")
        return obj

    def get_media(self, url: str, filename: str,
                  subdir: str | None = None) -> None:
        dest_dir = os.path.join(self.media_dir, subdir) if subdir else self.media_dir
        dest = os.path.join(dest_dir, filename)
        if os.path.exists(dest):
            return  # already on disk from a previous crawl run
        meta, content = self.fetch(url, binary=True)
        if meta is None:
            return
        if meta["status"] == 200 and content:
            os.makedirs(dest_dir, exist_ok=True)
            with open(dest, "wb") as f:
                f.write(content)
            print(f"  [img] {meta['size']:>8d}  {url}")
        else:
            print(f"  [{meta['status']}] (img) {url}")

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _collect_player_ids(obj, into: set[int]) -> None:
        if isinstance(obj, dict):
            pl = obj.get("player")
            if isinstance(pl, dict) and "id" in pl:
                into.add(pl["id"])
            for key in ("assist1", "assist2", "playerIn", "playerOut"):
                v = obj.get(key)
                if isinstance(v, dict) and "id" in v:
                    into.add(v["id"])
            for v in obj.values():
                Crawler._collect_player_ids(v, into)
        elif isinstance(obj, list):
            for v in obj:
                Crawler._collect_player_ids(v, into)

    # ------------------------------------------------------------------- run
    def run(self) -> None:
        eid = self.event_id

        # --- event core first: needed for slug + context ids -------------
        meta, content = self.fetch(f"{API}/event/{eid}")
        if not content:
            raise SystemExit(f"could not fetch event {eid} (status "
                             f"{meta.get('status') if meta else '?'})")
        event = json.loads(content)
        strip_field_translations(event)
        ev = event.get("event", {}) or {}
        home = ev.get("homeTeam", {}) or {}
        away = ev.get("awayTeam", {}) or {}
        home_slug = SLUG_NORMALISE.get(
            home.get("slug", "home").replace("-", "_"),
            home.get("slug", "home").replace("-", "_"),
        )
        away_slug = SLUG_NORMALISE.get(
            away.get("slug", "away").replace("-", "_"),
            away.get("slug", "away").replace("-", "_"),
        )
        slug = f"{home_slug}_v_{away_slug}"
        self._setup(slug)
        self._write_source("event.json", event)
        print(f"== {slug} (event {eid}) ==")

        # context ids + country mappings for team/manager (known from event core)
        tour = ev.get("tournament", {}) or {}
        self.unique_tournament_id = (tour.get("uniqueTournament", {}) or {}).get("id")
        self.season_id = (ev.get("season", {}) or {}).get("id")
        for side_key, country in (("homeTeam", home_slug), ("awayTeam", away_slug)):
            t = ev.get(side_key, {}) or {}
            if t.get("id"):
                self.team_ids.add(t["id"])
                self.team_country[t["id"]] = country
            m = t.get("manager") or {}
            if m.get("id"):
                self.manager_ids.add(m["id"])
                self.manager_country[m["id"]] = country

        # --- event sub-endpoints (the useful ones only) ------------------
        endpoints = {
            "statistics": "statistics.json",
            "lineups": "lineups.json",
            "incidents": "incidents.json",
            "graph": "graph.json",
            "shotmap": "shots.json",
            "best-players/summary": "best_players.json",
            "comments": "comments.json",
            "highlights": "highlights.json",
        }
        results: dict[str, object] = {}
        for suffix, name in endpoints.items():
            results[suffix] = self.get_json(f"{API}/event/{eid}/{suffix}", name)

        # momentum.json is just the graph (attack-momentum) series
        graph = results.get("graph")
        if graph is not None:
            self._write_source("momentum.json", graph)

        # --- discover players from lineups + incidents + best players ----
        for key in ("lineups", "incidents", "best-players/summary"):
            r = results.get(key)
            if isinstance(r, dict):
                self._collect_player_ids(r, self.player_ids)

        # Build player → country mapping from lineups (home/away sides)
        lineups = results.get("lineups")
        if isinstance(lineups, dict):
            for side_key, country in (("home", home_slug), ("away", away_slug)):
                side = lineups.get(side_key, {}) or {}
                for entry in (side.get("players") or []) + (side.get("missingPlayers") or []):
                    pid = (entry.get("player") or {}).get("id")
                    if pid:
                        self.player_country[pid] = country

        print(f"== {len(self.player_ids)} players, {len(self.team_ids)} teams, "
              f"{len(self.manager_ids)} managers ==")

        # --- player headshots ------------------------------------------------
        for pid in sorted(self.player_ids):
            country = self.player_country.get(pid)
            self.get_media(f"{API}/player/{pid}/image", f"player_{pid}.png",
                           subdir=country)

        # --- team crests + manager photos + tournament logo ------------------
        for tid in sorted(self.team_ids):
            country = self.team_country.get(tid)
            self.get_media(f"{API}/team/{tid}/image", f"team_{tid}.png",
                           subdir=country)
        for mid in sorted(self.manager_ids):
            country = self.manager_country.get(mid)
            self.get_media(f"{API}/manager/{mid}/image", f"manager_{mid}.png",
                           subdir=country)
        if self.unique_tournament_id:
            # Tournament logo is shared — lives directly in media/, no subdir
            self.get_media(
                f"{API}/unique-tournament/{self.unique_tournament_id}/image",
                "unique_tournament.png",
            )

        # --- group standings (corpus context) ----------------------------
        if self.unique_tournament_id and self.season_id:
            self.get_json(
                f"{API}/unique-tournament/{self.unique_tournament_id}/"
                f"season/{self.season_id}/standings/total",
                "standings_total.json",
            )

        # --- aggregate into metadata.json --------------------------------
        from aggregate import aggregate
        aggregate(self.out, prune=self.prune_sources)
        print(f"\nDONE -> {self.out}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Lean SofaScore match crawler.")
    ap.add_argument("--event-id", type=int, default=15186858)
    ap.add_argument("--out", default="data",
                    help="Base dir (git-ignored); a <home_v_away>/ subdir is "
                         "created in it.")
    ap.add_argument("--delay", type=float, default=0.4)
    ap.add_argument("--prune-sources", action="store_true",
                    help="After aggregating, delete source files already "
                         "represented in match.json (keeps comments, "
                         "best_players, lineups, shots, statistics).")
    args = ap.parse_args()
    Crawler(args.event_id, args.out, args.delay, args.prune_sources).run()


if __name__ == "__main__":
    main()
