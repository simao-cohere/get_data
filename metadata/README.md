# SofaScore match metadata crawler

Lean, match-scoped crawler + aggregator for a single SofaScore event. It fetches
only the metadata that is useful for **video retrieval / multimodal QA over match
footage**, then consolidates it into one `metadata.json` per match.

Reference match:

- **Portugal 5–0 Uzbekistan** — FIFA World Cup, Group K (NRG Stadium, Houston)
- Match page: `https://www.sofascore.com/football/match/uzbekistan-portugal/eUbsyUb#id:15186858`
- Event id: **15186858**

## The access trick

SofaScore's API (`https://api.sofascore.com/api/v1`) sits behind a **Varnish**
edge that returns `403 {"reason":"challenge"}` to ordinary clients. Getting
`200`s requires two things:

1. A real browser **TLS/JA3 fingerprint** — provided by
   [`curl_cffi`](https://github.com/yifeikong/curl_cffi) with
   `impersonate="chrome120"`.
2. The header **`X-Requested-With: XMLHttpRequest`** — this is the actual
   gatekeeper. A `Referer`/`Origin` of `sofascore.com` is sent too, but
   `X-Requested-With` is what flips the edge from 403 → 200.

No browser, proxy, or cookies are needed.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# crawl + aggregate in one shot (defaults to event 15186858)
.venv/bin/python crawl.py
# options: --event-id <id>  --out <base_dir>  --delay <seconds>  --prune-sources

# re-aggregate from already-downloaded sources (no network)
.venv/bin/python aggregate.py data/portugal_v_uzbekistan

# aggregate then delete the redundant raw sources (see "Source pruning")
.venv/bin/python aggregate.py data/portugal_v_uzbekistan --prune-sources
```

`crawl.py` reads the event, derives the match slug `<home>_v_<away>`, writes a
`<slug>/` directory under `--out` (default **`data/`**, which is git-ignored),
then calls `aggregate()` automatically. Re-running is idempotent:
`manifest.jsonl` is truncated at start and every file is rewritten in place.

Crawled matches accumulate under `data/` (one dir per match) and are kept out of
version control — only the code, docs, and `match_ids.txt` are committed.

## Batch crawl (all World Cup matches)

`run_crawl_all.py` walks `match_ids.txt` (every WC 2026 match, derived from the
season API) and crawls + aggregates + prunes each finished match, skipping any
already done. It is resumable and polite (per-request + per-match delays).

```bash
cd /root/repos/get_data/metadata
../.venv/bin/python run_crawl_all.py                # crawl all remaining finished matches
../.venv/bin/python run_crawl_all.py --refresh-ids  # rebuild match_ids.txt from the API first
```

or via the Cursor CLI from the repo root (reads context + task spec):

```bash
cursor-agent "Read metadata/AGENTS.md. Execute metadata/crawl_all.md."
```

`progress.json` **and** an existing `data/<slug>/metadata.json` both count as
"done", so re-running never repeats a match. Knobs (env vars): `PER_REQUEST_DELAY`
(default 1.0s), `MATCH_DELAY` (10s), `LIMIT`, `ONLY` (slug substring),
`INCLUDE_NOTSTARTED=1`. Logs/reports/progress land in `logs/`, `reports/`,
`progress.json` (all git-ignored).

`match_ids.txt` columns: `event_id  slug  status  round`. Only `finished`
matches are crawled (not-yet-played have no usable metadata).

## What is collected

Only the high-signal endpoints. Everything is time-resolvable or a lookup needed
to build the timeline.

| Area | Endpoints | Why |
|------|-----------|-----|
| Event core | `/event/{id}` | teams, score, kickoff epochs, venue, referee, managers |
| Timeline | `incidents`, `shotmap`, `graph` (momentum), `comments`, `highlights` | minute/second-resolved events + clip URLs |
| Aggregates | `statistics` (ALL/1ST/2ND), `lineups`, `best-players/summary` | team stats, XI/bench, per-player ratings |
| Per player (×53) | `event/{id}/player/{pid}/statistics`, `/player/{pid}/image` | in-match stats + headshot |
| Context | `unique-tournament/{ut}/season/{s}/standings/total` | group table |
| Media | player photos, team crests, manager photos, tournament logo | face/team recognition assets |

### Deliberately **not** collected

These were judged low-value for video QA (and bulky), so the crawler skips them:
`heatmaps`, `average-positions`, player career `profile`, `h2h`, `pregame-form`,
`team-streaks`, all `odds/*` and `predictions/votes`, team
recent/upcoming/performance/season stats, and the standalone
referee/venue/tournament **profile** JSON (that info is already embedded in the
event object).

`fieldTranslations` (per-language name dictionaries) are stripped from every
JSON on the fly — only English names + unicode in actual foreign names remain.

## Output layout (per match)

```
data/                           # git-ignored; accumulates one dir per match
 <slug>/                        # e.g. data/portugal_v_uzbekistan/
  metadata.json                 # <- the deliverable: aggregated, video-aligned
  media/                        # player_<id>.png, team_<id>.png, manager_<id>.png,
                                #   unique_tournament.png
  sources/                      # raw JSON metadata.json is built from
    statistics.json lineups.json shots.json          # kept (partial in metadata)
    comments.json best_players.json                  # kept (not in metadata)
    players/<pid>_event_statistics.json              # kept (not in metadata)
    event.json incidents.json momentum.json          # pruned by --prune-sources
    highlights.json standings_total.json graph.json  #   (fully in metadata.json)
  manifest.jsonl                # one line per request: url, status, size, headers, ts
  summary.json                  # discovered ids + status counts
```

By default `sources/` is kept so `metadata.json` can be regenerated offline with
`aggregate.py` (e.g. after a schema change), without re-hitting the API.

### Source pruning

`--prune-sources` deletes the raw files whose information is **fully represented
in `metadata.json`** and keeps only the detail-bearing ones not (fully) captured
there:

- **kept**: `comments.json` (commentary), `best_players.json`, per-player
  `players/*_event_statistics.json`, plus `lineups.json` / `shots.json` /
  `statistics.json` (metadata keeps only a summary of these).
- **deleted**: `event.json` (→ `match`), `incidents.json` (→ `events`),
  `graph.json` (duplicate of `momentum.json`), `momentum.json` (→ `windows` +
  inlined `momentum`), `highlights.json` (→ `events[].clip_url` +
  `feature_videos`), `standings_total.json` (→ `group_standings`).

Caveat: pruning removes inputs `aggregate.py` needs, so rebuilding `metadata.json`
after a prune requires a re-crawl. A few minor raw fields are not carried over
(team colors, incident pitch coordinates, standings for other groups).

## `metadata.json` schema

| Key | Contents |
|-----|----------|
| `match` | ids, competition/stage/round, both teams (name, code, ranking, manager), final + per-half score, `kickoff_utc`, `second_half_kickoff_utc`, injury time, venue, referee, attendance |
| `video_sync` | `clip_seconds`, the kickoff epochs, and a `files` stub to map `event.match_seconds` → time within each video file |
| `players` | `id → {name, team, position, shirt, nationality, starter, rating, minutes_played, on_minute, off_minute}` |
| `events` | unified, time-ordered timeline (goals, shots, cards, subs, VAR, unmatched highlight clips). Each has `match_seconds` (exact for shots, approx for incidents), `period`, a generated NL `text`, and `clip_url` when a highlight matched. Sorted by `(period, match_seconds)` |
| `feature_videos` | non-clip highlight media (full-match highlight reel, press conferences) — `{title, subtitle, url, media_type}` |
| `windows` | momentum-dominance intervals (`team`, `from_minute`, `to_minute`, `peak`) |
| `momentum` | raw attack-momentum series (`{minute, value}` per point) |
| `team_stats` | `ALL`/`1ST`/`2ND` → `{stat_key: {name, home, away}}` |
| `group_standings` | the match's group table |
| `media` | id → relative path for each downloaded image |
| `counts` | quick tallies (events, goals, shots, cards, subs, clips, players) |

### Timestamp alignment notes

- Shots carry an **exact** `timeSeconds` (`match_seconds_exact: true`). Cards,
  subs and VAR only have a minute, so `match_seconds` is a mid-minute estimate
  (`match_seconds_exact: false`).
- `match_seconds` is match-clock seconds. To reach a position in a video file,
  add that file's kickoff offset (and the half-time gap for second-half events) —
  fill `video_sync.files` per video. `second_half_kickoff_utc - kickoff_utc`
  gives the real elapsed gap across half-time for the live broadcast.
- Highlight clip URLs (`event.clip_url`) point directly at SofaScore-hosted
  video for that moment.

## Adapting to other matches

`crawl.py` / `aggregate.py` are generic — pass `--event-id` for any SofaScore
football match and you get the same `<home>_v_<away>/metadata.json` layout,
ready to align against the corresponding match video in
`/data/1d/simao/football_downloads/`.
