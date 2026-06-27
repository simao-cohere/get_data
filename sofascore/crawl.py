#!/usr/bin/env python3
"""Exhaustive SofaScore metadata crawler for a single match (event).

SofaScore's public API (``api.sofascore.com/api/v1``) sits behind a Varnish
edge that returns ``403 {"reason":"challenge"}`` to plain clients. Two things
are required to get ``200`` responses:

  1. a real browser TLS/JA3 fingerprint  -> provided by ``curl_cffi`` impersonate
  2. the header ``X-Requested-With: XMLHttpRequest``  (the actual gatekeeper)

Given those, every documented v1 endpoint is reachable.

The crawler is *match-scoped exhaustive*: it fetches the event and every
endpoint referenced by / derivable from it (statistics, lineups, incidents,
momentum graph, shotmap, best players, managers, odds, votes/predictions,
per-player event statistics + heatmaps + profiles, both teams, the referee,
the venue, and the tournament/season/standings context), plus media assets.

To avoid crawling the entire site, recursion is bounded to entities directly
attached to this match (it does NOT expand each player's whole club history,
every opponent's other matches, etc.). Every response is stored verbatim under
``raw/`` with a metadata sidecar, and a clean categorised copy is written under
the directory tree required by the task. A ``manifest.jsonl`` records one line
per request (url, method, status, content-type, size, headers, timestamp).

Usage:
    python crawl.py [--event-id 15186858] [--out event] [--delay 0.4]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from curl_cffi import requests

API = "https://api.sofascore.com/api/v1"
WEB = "https://www.sofascore.com"
IMPERSONATE = "chrome120"

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


def safe_name(url: str) -> str:
    """Turn an API URL into a filesystem-safe stem."""
    s = url.replace(API + "/", "").replace(WEB + "/", "web/")
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"[^A-Za-z0-9._/-]", "_", s)
    s = s.replace("/", "__")
    return s[:180]


class Crawler:
    def __init__(self, event_id: int, out_dir: str, delay: float = 0.4):
        self.event_id = event_id
        self.out = os.path.abspath(out_dir)
        self.delay = delay
        self.session = requests.Session(impersonate=IMPERSONATE)
        self.session.headers.update(BASE_HEADERS)
        self.visited: set[str] = set()
        self.manifest_path = os.path.join(self.out, "manifest.jsonl")
        self.raw_dir = os.path.join(self.out, "raw")
        # discovered ids (filled as we crawl)
        self.player_ids: set[int] = set()
        self.team_ids: set[int] = set()
        self.manager_ids: set[int] = set()
        self.referee_id: int | None = None
        self.venue_id: int | None = None
        self.tournament_id: int | None = None
        self.unique_tournament_id: int | None = None
        self.season_id: int | None = None
        for d in [
            "", "raw", "heatmaps", "players", "teams", "standings", "odds",
            "predictions", "referee", "venue", "tournament", "media",
        ]:
            os.makedirs(os.path.join(self.out, d), exist_ok=True)
        # fresh manifest each run (raw/ files are simply overwritten by stem)
        open(self.manifest_path, "w").close()

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
                # retry transient blocks / server errors
                if r.status_code in (403, 429, 500, 502, 503) and attempt < retries:
                    time.sleep(min(2 ** attempt, 12))
                    self.visited.discard(url)
                    self.visited.add(url)
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

    def _record(self, meta: dict) -> None:
        with open(self.manifest_path, "a") as f:
            f.write(json.dumps(meta) + "\n")

    def _write_raw(self, url: str, meta: dict, content) -> None:
        stem = safe_name(url)
        is_bytes = isinstance(content, (bytes, bytearray))
        ext = ".bin" if is_bytes else ".json"
        if content is not None:
            mode = "wb" if is_bytes else "w"
            with open(os.path.join(self.raw_dir, stem + ext), mode) as f:
                f.write(content)
        with open(os.path.join(self.raw_dir, stem + ".meta.json"), "w") as f:
            json.dump(meta, f, indent=2)

    def get_json(self, url: str, category: str | None = None):
        """Fetch a JSON endpoint, store it, and return the parsed object (or None).

        ``fieldTranslations`` blocks (per-language name dictionaries we don't
        need) are stripped from the stored JSON; the English ``name`` fields and
        any unicode in actual (foreign) names are kept.
        """
        meta, content = self.fetch(url)
        if meta is None:
            return None
        self._record(meta)
        parsed = None
        body = content
        if meta["status"] == 200 and content is not None:
            try:
                parsed = json.loads(content)
                strip_field_translations(parsed)
                body = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                parsed, body = None, content
        self._write_raw(url, meta, body)
        if meta["status"] != 200 or content is None:
            print(f"  [{meta['status']}] {url}")
            return None
        print(f"  [200] {meta['size']:>8d}  {url}")
        if category:
            dst = os.path.join(self.out, category)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "w") as f:
                f.write(body)
        return parsed

    def get_media(self, url: str, dst_rel: str) -> None:
        meta, content = self.fetch(url, binary=True)
        if meta is None:
            return
        self._record(meta)
        self._write_raw(url, meta, content)
        if meta["status"] == 200 and content:
            dst = os.path.join(self.out, dst_rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(content)
            print(f"  [img] {meta['size']:>8d}  {url}")
        else:
            print(f"  [{meta['status']}] (img) {url}")

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _collect_player_ids(obj, into: set[int]) -> None:
        """Walk a JSON structure collecting player ids."""
        if isinstance(obj, dict):
            if obj.get("player") and isinstance(obj["player"], dict) and "id" in obj["player"]:
                into.add(obj["player"]["id"])
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
        print("== match page (inline data) ==")
        # Save the human match page HTML for completeness / discovery. Stored as
        # .html (not .json) so it is not treated as a structured API response.
        meta, content = self.fetch(
            f"{WEB}/football/match/uzbekistan-portugal/eUbsyUb"
        )
        if meta:
            self._record(meta)
            if content is not None:
                with open(os.path.join(self.raw_dir, "match_page.html"), "w") as f:
                    f.write(content)
            with open(os.path.join(self.raw_dir, "match_page.html.meta.json"), "w") as f:
                json.dump(meta, f, indent=2)

        print("== event core ==")
        event = self.get_json(f"{API}/event/{eid}", "event.json")
        ev = event.get("event", {}) if event else {}
        # extract context ids
        tour = ev.get("tournament", {}) or {}
        self.tournament_id = tour.get("id")
        self.unique_tournament_id = (tour.get("uniqueTournament", {}) or {}).get("id")
        self.season_id = (ev.get("season", {}) or {}).get("id")
        for side in ("homeTeam", "awayTeam"):
            t = ev.get(side, {}) or {}
            if t.get("id"):
                self.team_ids.add(t["id"])
        if ev.get("referee", {}).get("id"):
            self.referee_id = ev["referee"]["id"]
        if ev.get("venue", {}).get("id"):
            self.venue_id = ev["venue"]["id"]
        # venue + referee subtrees straight from event
        if ev.get("venue"):
            with open(os.path.join(self.out, "venue", "venue_from_event.json"), "w") as f:
                json.dump(ev["venue"], f, indent=2)
        if ev.get("referee"):
            with open(os.path.join(self.out, "referee", "referee_from_event.json"), "w") as f:
                json.dump(ev["referee"], f, indent=2)

        print("== event sub-endpoints ==")
        event_endpoints = {
            "statistics": "statistics.json",
            "lineups": "lineups.json",
            "incidents": "incidents.json",
            "graph": "graph.json",
            "managers": "managers.json",
            "shotmap": "shots.json",
            "best-players/summary": "best_players.json",
            "h2h": "h2h.json",
            "pregame-form": "pregame_form.json",
            "team-streaks": "team_streaks.json",
            "comments": "comments.json",
            "highlights": "highlights.json",
            "average-positions": "average_positions.json",
            "win-probability": "predictions/win_probability.json",
            "votes": "predictions/votes.json",
            "provider/1/winning-odds": "predictions/winning_odds.json",
            "odds/1/all": "odds/all.json",
            "odds/1/featured": "odds/featured.json",
            "graph/win-probability": "predictions/graph_win_probability.json",
            "fan-rating": "predictions/fan_rating.json",
        }
        results: dict[str, object] = {}
        for suffix, cat in event_endpoints.items():
            results[suffix] = self.get_json(f"{API}/event/{eid}/{suffix}", cat)

        # momentum.json is an alias of graph.json (the momentum graph)
        graph_path = os.path.join(self.out, "graph.json")
        if os.path.exists(graph_path):
            with open(graph_path) as f:
                data = f.read()
            with open(os.path.join(self.out, "momentum.json"), "w") as f:
                f.write(data)

        # ---- discover player ids from lineups + incidents + best players ----
        lineups = results.get("lineups")
        if isinstance(lineups, dict):
            self._collect_player_ids(lineups, self.player_ids)
        incidents = results.get("incidents")
        if isinstance(incidents, dict):
            self._collect_player_ids(incidents, self.player_ids)
        if isinstance(results.get("best-players/summary"), dict):
            self._collect_player_ids(results["best-players/summary"], self.player_ids)
        # managers
        mgr = results.get("managers")
        if isinstance(mgr, dict):
            for k in ("homeManager", "awayManager"):
                m = mgr.get(k)
                if isinstance(m, dict) and m.get("id"):
                    self.manager_ids.add(m["id"])

        print(f"== discovered: {len(self.player_ids)} players, "
              f"{len(self.team_ids)} teams, {len(self.manager_ids)} managers ==")

        print("== per-player event stats + heatmaps + profiles ==")
        has_player_stats = ev.get("hasEventPlayerStatistics", True)
        for pid in sorted(self.player_ids):
            if has_player_stats:
                self.get_json(
                    f"{API}/event/{eid}/player/{pid}/statistics",
                    f"players/{pid}_event_statistics.json",
                )
            self.get_json(
                f"{API}/event/{eid}/player/{pid}/heatmap",
                f"heatmaps/{pid}.json",
            )
            self.get_json(f"{API}/player/{pid}", f"players/{pid}_profile.json")
            self.get_media(f"{API}/player/{pid}/image", f"media/player_{pid}.png")

        print("== teams (info, squad, recent matches, form) ==")
        for tid in sorted(self.team_ids):
            self.get_json(f"{API}/team/{tid}", f"teams/{tid}.json")
            self.get_json(f"{API}/team/{tid}/players", f"teams/{tid}_players.json")
            self.get_json(
                f"{API}/team/{tid}/events/last/0", f"teams/{tid}_recent_events.json")
            self.get_json(
                f"{API}/team/{tid}/events/next/0", f"teams/{tid}_upcoming_events.json")
            self.get_json(
                f"{API}/team/{tid}/performance", f"teams/{tid}_performance.json")
            if self.unique_tournament_id and self.season_id:
                self.get_json(
                    f"{API}/team/{tid}/unique-tournament/{self.unique_tournament_id}/"
                    f"season/{self.season_id}/statistics/overall",
                    f"teams/{tid}_season_statistics.json")
            self.get_media(f"{API}/team/{tid}/image", f"media/team_{tid}.png")

        print("== managers ==")
        for mid in sorted(self.manager_ids):
            self.get_json(f"{API}/manager/{mid}", f"teams/manager_{mid}.json")
            self.get_media(f"{API}/manager/{mid}/image", f"media/manager_{mid}.png")

        print("== referee ==")
        if self.referee_id:
            rid = self.referee_id
            self.get_json(f"{API}/referee/{rid}", f"referee/{rid}.json")
            self.get_json(
                f"{API}/referee/{rid}/events/last/0",
                f"referee/{rid}_recent_events.json",
            )

        print("== venue ==")
        if self.venue_id:
            self.get_json(f"{API}/venue/{self.venue_id}", "venue/venue.json")

        print("== tournament / season / standings ==")
        if self.tournament_id:
            self.get_json(
                f"{API}/tournament/{self.tournament_id}",
                "tournament/tournament.json",
            )
        utid, sid = self.unique_tournament_id, self.season_id
        if utid:
            self.get_json(
                f"{API}/unique-tournament/{utid}",
                "tournament/unique_tournament.json",
            )
            self.get_media(
                f"{API}/unique-tournament/{utid}/image",
                "media/unique_tournament.png",
            )
        if utid and sid:
            self.get_json(
                f"{API}/unique-tournament/{utid}/season/{sid}/info",
                "tournament/season_info.json",
            )
            for kind in ("total", "home", "away"):
                self.get_json(
                    f"{API}/unique-tournament/{utid}/season/{sid}/standings/{kind}",
                    f"standings/{kind}.json",
                )
            self.get_json(
                f"{API}/unique-tournament/{utid}/season/{sid}/events/round/"
                f"{(ev.get('roundInfo') or {}).get('round', 1)}",
                "tournament/round_events.json",
            )

        # National-team crests (downloaded above as media/team_<id>.png) double as
        # the country flags, so no separate flag asset is needed here.

        self._write_summary()
        print("\nDONE.")

    def _write_summary(self) -> None:
        # count manifest lines / statuses
        statuses: dict[str, int] = {}
        total = 0
        if os.path.exists(self.manifest_path):
            with open(self.manifest_path) as f:
                for line in f:
                    total += 1
                    try:
                        s = str(json.loads(line)["status"])
                    except Exception:
                        s = "?"
                    statuses[s] = statuses.get(s, 0) + 1
        summary = {
            "event_id": self.event_id,
            "generated_at": now_iso(),
            "requests_total": total,
            "status_counts": statuses,
            "discovered": {
                "players": sorted(self.player_ids),
                "teams": sorted(self.team_ids),
                "managers": sorted(self.manager_ids),
                "referee_id": self.referee_id,
                "venue_id": self.venue_id,
                "tournament_id": self.tournament_id,
                "unique_tournament_id": self.unique_tournament_id,
                "season_id": self.season_id,
            },
        }
        with open(os.path.join(self.out, "summary.json"), "w") as f:
            json.dump(summary, f, indent=2)
        print("\n== summary ==")
        print(json.dumps(summary["status_counts"], indent=2))
        print(f"players={len(self.player_ids)} teams={len(self.team_ids)} "
              f"managers={len(self.manager_ids)} total_requests={total}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--event-id", type=int, default=15186858)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "event"))
    ap.add_argument("--delay", type=float, default=0.4)
    args = ap.parse_args()
    Crawler(args.event_id, args.out, args.delay).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
