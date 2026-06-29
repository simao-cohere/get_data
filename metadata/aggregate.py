#!/usr/bin/env python3
"""Aggregate the lean SofaScore source files for one match into a single,
video-aligned ``metadata.json``.

Input  : <match_dir>/sources/*.json   (written by crawl.py)
Output : <match_dir>/metadata.json

The aggregate is built to support semantic video retrieval + multimodal QA over
30-second match clips. It produces:

  match        - core context (teams, score, kickoff epochs, venue, referee)
  video_sync   - stub for mapping match-time -> video-time per video file
  players      - id -> {name, team, position, shirt, rating, on/off minute}
  events       - unified, time-ordered timeline merging shots + incidents +
                 highlight clips; each event carries match_seconds + NL text
  windows      - momentum-dominance intervals
  team_stats   - team aggregates for ALL / 1ST / 2ND
  group_standings - the match's group table (if available)

Usage:
    python aggregate.py <match_dir>      # e.g. data/portugal_v_uzbekistan
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone

# Penalty area heuristic on SofaScore's 0-100 shot coordinates (attacking goal
# near x=0; box is ~16.5 m deep ≈ 17% of pitch length, ~40 m wide ≈ y 21-79).
BOX_X_MAX = 17.0
BOX_Y_MIN, BOX_Y_MAX = 21.0, 79.0
MOMENTUM_THRESHOLD = 25  # |attack momentum| above this counts as "dominance"

SHOT_TYPE_MAP = {
    "goal": "goal",
    "save": "shot_saved",
    "miss": "shot_off_target",
    "block": "shot_blocked",
    "post": "shot_woodwork",
}
SHOT_LABEL = {
    "shot_saved": "shot saved",
    "shot_off_target": "shot off target",
    "shot_blocked": "shot blocked",
    "shot_woodwork": "hit the woodwork",
    "shot": "shot",
}


def _load(src_dir: str, name: str):
    path = os.path.join(src_dir, name)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _period_for(minute) -> int:
    return 1 if (minute or 0) <= 45 else 2


def _approx_seconds(minute):
    """Mid-of-minute estimate for events lacking an exact timeSeconds."""
    return (minute * 60 - 30) if minute else None


def _zone(coords) -> str:
    x = (coords or {}).get("x")
    y = (coords or {}).get("y")
    if x is not None and x <= BOX_X_MAX and BOX_Y_MIN <= (y if y is not None else 50) <= BOX_Y_MAX:
        return "penalty_area"
    return "outside_box"


# --------------------------------------------------------------------- build
def build_match(event: dict) -> dict:
    ev = event["event"]
    tour = ev.get("tournament", {}) or {}
    ut = tour.get("uniqueTournament", {}) or {}
    home = ev.get("homeTeam", {}) or {}
    away = ev.get("awayTeam", {}) or {}
    hs = ev.get("homeScore", {}) or {}
    aws = ev.get("awayScore", {}) or {}
    venue = ev.get("venue", {}) or {}
    ref = ev.get("referee", {}) or {}
    tm = ev.get("time", {}) or {}
    return {
        "event_id": ev.get("id"),
        "slug": f"{home.get('slug')}_v_{away.get('slug')}".replace("-", "_"),
        "competition": ut.get("name") or tour.get("name"),
        "stage": tour.get("groupName"),
        "season": (ev.get("season") or {}).get("name"),
        "round": (ev.get("roundInfo") or {}).get("round"),
        "home": {
            "id": home.get("id"), "name": home.get("name"),
            "code": home.get("nameCode"), "ranking": home.get("ranking"),
            "manager": (home.get("manager") or {}).get("name"),
        },
        "away": {
            "id": away.get("id"), "name": away.get("name"),
            "code": away.get("nameCode"), "ranking": away.get("ranking"),
            "manager": (away.get("manager") or {}).get("name"),
        },
        "final_score": f"{hs.get('current')}-{aws.get('current')}",
        "score_by_period": {
            "first_half": f"{hs.get('period1')}-{aws.get('period1')}",
            "second_half": f"{hs.get('period2')}-{aws.get('period2')}",
        },
        "kickoff_utc": ev.get("startTimestamp"),
        "second_half_kickoff_utc": ev.get("currentPeriodStartTimestamp"),
        "injury_time_first_half": tm.get("injuryTime1"),
        "injury_time_second_half": tm.get("injuryTime2"),
        "attendance": ev.get("attendance"),
        "venue": {
            "name": venue.get("name"),
            "city": (venue.get("city") or {}).get("name"),
            "capacity": venue.get("capacity"),
            "coordinates": venue.get("venueCoordinates"),
        },
        "referee": {
            "name": ref.get("name"),
            "country": (ref.get("country") or {}).get("name"),
        },
        "status": (ev.get("status") or {}).get("type"),
    }


def build_players(lineups, incidents, event) -> dict:
    players: dict[str, dict] = {}
    if lineups:
        for side in ("home", "away"):
            block = lineups.get(side) or {}
            for p in block.get("players", []):
                pl = p.get("player", {}) or {}
                pid = pl.get("id")
                if pid is None:
                    continue
                st = p.get("statistics", {}) or {}
                sub = p.get("substitute", False)
                players[str(pid)] = {
                    "name": pl.get("name"),
                    "team": side,
                    "position": p.get("position") or pl.get("position"),
                    "shirt": p.get("shirtNumber") or pl.get("jerseyNumber"),
                    "nationality": (pl.get("country") or {}).get("name"),
                    "starter": not sub,
                    "rating": st.get("rating"),
                    "minutes_played": st.get("minutesPlayed"),
                    "on_minute": 0 if not sub else None,
                    "off_minute": None,
                }
    # on/off minutes from substitutions
    for inc in incidents or []:
        if inc.get("incidentType") == "substitution":
            t = inc.get("time")
            pin = str((inc.get("playerIn") or {}).get("id"))
            pout = str((inc.get("playerOut") or {}).get("id"))
            if pin in players:
                players[pin]["on_minute"] = t
            if pout in players:
                players[pout]["off_minute"] = t
    return players


def build_events(event, incidents, shots, highlights) -> list:
    ev = event["event"]
    home_code = (ev.get("homeTeam") or {}).get("nameCode")
    away_code = (ev.get("awayTeam") or {}).get("nameCode")
    events: list[dict] = []
    shot_index: dict = {}

    # 1) shots (richest: exact timeSeconds, xg, coordinates)
    for s in shots or []:
        st = s.get("shotType")
        typ = SHOT_TYPE_MAP.get(st, "shot")
        obj = {
            "uid": f"shot_{s.get('id')}",
            "type": typ,
            "team": "home" if s.get("isHome") else "away",
            "player_id": (s.get("player") or {}).get("id"),
            "minute": s.get("time"),
            "added_time": s.get("addedTime", 0),
            "period": _period_for(s.get("time")),
            "match_seconds": s.get("timeSeconds"),
            "match_seconds_exact": s.get("timeSeconds") is not None,
            "xg": s.get("xg"),
            "xgot": s.get("xgot"),
            "body_part": s.get("bodyPart"),
            "situation": s.get("situation"),
            "zone": _zone(s.get("playerCoordinates")),
            "on_target": st in ("goal", "save"),
            "shot_outcome": st,
        }
        events.append(obj)
        shot_index.setdefault(
            ((s.get("player") or {}).get("id"), s.get("time")), []
        ).append(obj)

    used_goal_shots: set[str] = set()

    # 2) incidents (goals enrich matching shots; cards/subs/var are new events)
    for inc in incidents or []:
        it = inc.get("incidentType")
        t = inc.get("time")
        team = "home" if inc.get("isHome") else "away"
        if it == "goal":
            pid = (inc.get("player") or {}).get("id")
            assist = inc.get("assist1") or {}
            score_after = (
                f"{inc.get('homeScore')}-{inc.get('awayScore')}"
                if inc.get("homeScore") is not None else None
            )
            match = None
            for dt in (0, 1, -1, 2, -2):
                for cand in shot_index.get((pid, (t or 0) + dt), []):
                    if cand["type"] == "goal" and cand["uid"] not in used_goal_shots:
                        match = cand
                        break
                if match:
                    break
            if match:
                used_goal_shots.add(match["uid"])
                match["assist_player_id"] = assist.get("id")
                match["score_after"] = score_after
                match["goal_type"] = inc.get("incidentClass")
            else:  # own goal / penalty not present in shotmap
                events.append({
                    "uid": f"goal_{inc.get('id')}",
                    "type": "goal",
                    "team": team,
                    "player_id": pid,
                    "assist_player_id": assist.get("id"),
                    "minute": t,
                    "added_time": inc.get("addedTime", 0),
                    "period": _period_for(t),
                    "match_seconds": _approx_seconds(t),
                    "match_seconds_exact": False,
                    "score_after": score_after,
                    "goal_type": inc.get("incidentClass"),
                })
        elif it == "card":
            events.append({
                "uid": f"card_{inc.get('id')}",
                "type": "card_" + (inc.get("incidentClass") or "yellow"),
                "team": team,
                "player_id": (inc.get("player") or {}).get("id"),
                "minute": t,
                "added_time": inc.get("addedTime", 0),
                "period": _period_for(t),
                "match_seconds": _approx_seconds(t),
                "match_seconds_exact": False,
                "reason": inc.get("reason"),
            })
        elif it == "substitution":
            events.append({
                "uid": f"sub_{inc.get('id')}",
                "type": "substitution",
                "team": team,
                "player_in_id": (inc.get("playerIn") or {}).get("id"),
                "player_out_id": (inc.get("playerOut") or {}).get("id"),
                "minute": t,
                "added_time": inc.get("addedTime", 0),
                "period": _period_for(t),
                "match_seconds": _approx_seconds(t),
                "match_seconds_exact": False,
            })
        elif it == "varDecision":
            events.append({
                "uid": f"var_{inc.get('id')}",
                "type": "var",
                "team": team,
                "player_id": (inc.get("player") or {}).get("id"),
                "minute": t,
                "added_time": inc.get("addedTime", 0),
                "period": _period_for(t),
                "match_seconds": _approx_seconds(t),
                "match_seconds_exact": False,
                "decision": inc.get("incidentClass"),
            })
        # period / injuryTime markers are intentionally skipped

    _attach_highlights(events, highlights, home_code, away_code)
    events.sort(key=lambda e: (
        e.get("match_seconds") if e.get("match_seconds") is not None else 1e9
    ))
    return events


def _highlight_minute(title: str):
    """Parse the match minute from a highlight title.

    Handles stoppage time, e.g. ``"... 45+2'"`` -> base minute 45 (added 2).
    Returns (minute, added) or (None, 0).
    """
    m = re.search(r"(\d+)(?:\s*\+\s*(\d+))?'", title or "")
    if not m:
        return None, 0
    return int(m.group(1)), int(m.group(2) or 0)


def _attach_highlights(events, highlights, home_code, away_code) -> None:
    clip_targets = (
        "goal", "shot_saved", "shot_off_target", "shot_blocked",
        "shot_woodwork", "card_yellow", "card_red", "var",
    )
    seen_standalone: set = set()
    attached_keys: set = set()
    for h in highlights or []:
        if h.get("mediaType") != 1:  # 1 == video clip
            continue
        url = h.get("directStreamUrl") or h.get("url")
        title = (h.get("title") or "").strip()
        subtitle = (h.get("subtitle") or "").strip()
        minute, added = _highlight_minute(title)
        if minute is None:
            continue  # intro / full-reel clips: keep in sources, skip timeline
        side = None
        if home_code and f"({home_code})" in title:
            side = "home"
        elif away_code and f"({away_code})" in title:
            side = "away"

        best = None
        for e in events:
            if e.get("minute") is None or e.get("clip_url"):
                continue
            if side and e.get("team") != side:
                continue
            if e["type"] in clip_targets and abs((e["minute"] or 0) - minute) <= 1:
                best = e
                if e["type"] == "goal":
                    break
        base = re.sub(r"\s*\(replay\)\s*", "", subtitle, flags=re.I).lower()
        if best is not None:
            best["clip_url"] = url
            best["clip_title"] = title
            best["clip_subtitle"] = subtitle
            attached_keys.add((minute, side, base))
        else:
            # drop replays whose primary already attached to a real event,
            # and collapse remaining "(replay)" duplicates of the same moment
            key = (minute, side, base)
            if key in attached_keys or key in seen_standalone:
                continue
            seen_standalone.add(key)
            events.append({
                "uid": f"clip_{h.get('id')}",
                "type": "highlight_clip",
                "team": side,
                "minute": minute,
                "added_time": added,
                "period": _period_for(minute),
                "match_seconds": _approx_seconds(minute),
                "match_seconds_exact": False,
                "clip_url": url,
                "clip_title": title,
                "clip_subtitle": subtitle,
            })


def add_text(events, players, home_name, away_name) -> None:
    def nm(pid):
        p = players.get(str(pid))
        return p["name"] if p and p.get("name") else (f"#{pid}" if pid else "?")

    def tn(team):
        return home_name if team == "home" else (away_name if team == "away" else "")

    for e in events:
        t = e.get("minute")
        tm = tn(e.get("team"))
        typ = e["type"]
        if typ == "goal":
            if e.get("goal_type") == "ownGoal":
                s = f"{t}' OWN GOAL by {nm(e.get('player_id'))}"
                if e.get("score_after"):
                    s += f", {e['score_after']}"
                s += f" (counts for {tm})"
            else:
                s = f"{t}' GOAL - {nm(e.get('player_id'))} ({tm})"
                if e.get("score_after"):
                    s += f", {e['score_after']}"
                if e.get("assist_player_id"):
                    s += f", assist {nm(e['assist_player_id'])}"
                if e.get("body_part"):
                    s += f", {e['body_part']}"
                if e.get("situation") and e["situation"] != "regular":
                    s += f" ({e['situation']})"
            e["text"] = s
        elif typ.startswith("card_"):
            e["text"] = f"{t}' {typ.split('_', 1)[1].title()} card - {nm(e.get('player_id'))} ({tm})"
        elif typ == "substitution":
            e["text"] = (f"{t}' Substitution ({tm}) - {nm(e.get('player_in_id'))} on, "
                         f"{nm(e.get('player_out_id'))} off")
        elif typ == "var":
            e["text"] = f"{t}' VAR: {e.get('decision')} ({tm})"
        elif typ == "highlight_clip":
            e["text"] = f"{t or '?'}' {e.get('clip_title', '')} - {e.get('clip_subtitle', '')}".strip(" -")
        else:
            label = SHOT_LABEL.get(typ, "shot")
            s = f"{t}' {nm(e.get('player_id'))} ({tm}) {label}"
            if e.get("zone"):
                s += f" from {e['zone'].replace('_', ' ')}"
            if e.get("xg") is not None:
                s += f", xG {round(e['xg'], 2)}"
            e["text"] = s


def build_windows(momentum) -> list:
    windows = []
    cur = None
    for p in momentum or []:
        v = p.get("value", 0)
        minute = p.get("minute")
        side = "home" if v >= MOMENTUM_THRESHOLD else ("away" if v <= -MOMENTUM_THRESHOLD else None)
        prev_side = cur["team"] if cur else None
        if side != prev_side:
            if cur:
                windows.append(cur)
            cur = {"type": "momentum_dominance", "team": side,
                   "from_minute": minute, "to_minute": minute, "peak": v} if side else None
        elif cur:
            cur["to_minute"] = minute
            if abs(v) > abs(cur["peak"]):
                cur["peak"] = v
    if cur:
        windows.append(cur)
    return [w for w in windows if w and w.get("team")]


def build_team_stats(statistics) -> dict:
    out: dict = {}
    for period in statistics or []:
        pname = period.get("period")
        d = {}
        for g in period.get("groups", []):
            for it in g.get("statisticsItems", []):
                key = it.get("key")
                if not key:
                    continue
                d[key] = {
                    "name": it.get("name"),
                    "home": it.get("homeValue", it.get("home")),
                    "away": it.get("awayValue", it.get("away")),
                }
        out[pname] = d
    return out


def build_group_standings(standings_json, group_name):
    if not standings_json or not group_name:
        return None
    for grp in standings_json.get("standings", []):
        if grp.get("name") == group_name or grp.get("groupName") == group_name:
            rows = []
            for r in grp.get("rows", []):
                rows.append({
                    "position": r.get("position"),
                    "team": (r.get("team") or {}).get("name"),
                    "played": r.get("matches"),
                    "wins": r.get("wins"),
                    "draws": r.get("draws"),
                    "losses": r.get("losses"),
                    "goals_for": r.get("scoresFor"),
                    "goals_against": r.get("scoresAgainst"),
                    "points": r.get("points"),
                })
            return {"group": group_name, "rows": rows}
    return None


# ----------------------------------------------------------------- assemble
def aggregate(match_dir: str) -> str:
    src = os.path.join(match_dir, "sources")
    event = _load(src, "event.json")
    if not event or "event" not in event:
        raise SystemExit(f"no usable event.json under {src}")

    incidents = (_load(src, "incidents.json") or {}).get("incidents", [])
    shots = (_load(src, "shots.json") or {}).get("shotmap", [])
    highlights = (_load(src, "highlights.json") or {}).get("highlights", [])
    momentum = (_load(src, "momentum.json") or {}).get("graphPoints", [])
    statistics = (_load(src, "statistics.json") or {}).get("statistics", [])
    lineups = _load(src, "lineups.json")
    standings = _load(src, "standings_total.json")

    ev = event["event"]
    home_name = (ev.get("homeTeam") or {}).get("name")
    away_name = (ev.get("awayTeam") or {}).get("name")

    match = build_match(event)
    players = build_players(lineups, incidents, event)
    events = build_events(event, incidents, shots, highlights)
    add_text(events, players, home_name, away_name)
    windows = build_windows(momentum)
    team_stats = build_team_stats(statistics)
    group_standings = build_group_standings(match.get("stage") and standings, match.get("stage"))

    # which players actually have a media headshot on disk
    media_dir = os.path.join(match_dir, "media")
    media_index = {"players": {}, "teams": {}, "managers": {}, "tournament": None}
    if os.path.isdir(media_dir):
        for fn in os.listdir(media_dir):
            if fn.startswith("player_"):
                media_index["players"][fn[len("player_"):].split(".")[0]] = f"media/{fn}"
            elif fn.startswith("team_"):
                media_index["teams"][fn[len("team_"):].split(".")[0]] = f"media/{fn}"
            elif fn.startswith("manager_"):
                media_index["managers"][fn[len("manager_"):].split(".")[0]] = f"media/{fn}"
            elif fn.startswith("unique_tournament"):
                media_index["tournament"] = f"media/{fn}"

    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": 1,
        "match": match,
        "video_sync": {
            "clip_seconds": 30,
            "kickoff_utc": match["kickoff_utc"],
            "second_half_kickoff_utc": match["second_half_kickoff_utc"],
            "note": ("Fill 'files' with per-video kickoff offsets, then map "
                     "event.match_seconds -> video time. Anchor on kickoff + "
                     "scoreboard OCR + highlight clips; interpolate per half."),
            "files": {},
        },
        "counts": {
            "events": len(events),
            "goals": sum(1 for e in events if e["type"] == "goal"),
            "shots": sum(1 for e in events if e["type"].startswith("shot") or e["type"] == "goal"),
            "cards": sum(1 for e in events if e["type"].startswith("card_")),
            "substitutions": sum(1 for e in events if e["type"] == "substitution"),
            "highlight_clips": sum(1 for e in events if e.get("clip_url")),
            "players": len(players),
        },
        "players": players,
        "media": media_index,
        "events": events,
        "windows": windows,
        "team_stats": team_stats,
        "group_standings": group_standings,
    }

    out_path = os.path.join(match_dir, "metadata.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)
    print(f"wrote {out_path}: {metadata['counts']}")
    return out_path


if __name__ == "__main__":
    aggregate(os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "."))
