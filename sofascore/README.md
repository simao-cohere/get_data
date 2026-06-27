# SofaScore match metadata crawler

Exhaustive, match-scoped crawler for a single SofaScore event. Built for:

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
   gatekeeper. A `Referer`/`Origin` of `sofascore.com` is sent too for good
   measure, but `X-Requested-With` is what flips the edge from 403 → 200.

No browser, proxy, or cookies are needed.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python crawl.py            # defaults to event 15186858 -> ./event/
# options: --event-id, --out, --delay
```

The crawl is idempotent: re-running overwrites `event/` (the `manifest.jsonl`
is truncated at start and `raw/` files are keyed by URL).

## What is collected

Everything attached to the match, recursively discovered from the event object
and its lineups/incidents:

| Area | Endpoints |
|------|-----------|
| Event core | `/event/{id}` |
| Match stats | `statistics` (full/1st/2nd), `graph` (momentum), `average-positions` |
| Events | `incidents`, `shotmap`, `best-players/summary`, `comments`, `highlights` |
| Lineups | `lineups`, `managers` (formations, XI, bench, ratings, etc.) |
| Context | `h2h`, `pregame-form`, `team-streaks` |
| Odds / predictions | `odds/1/all`, `odds/1/featured`, `votes`, `provider/1/winning-odds` |
| Per player (×53) | `event/{id}/player/{pid}/statistics`, `.../heatmap`, `/player/{pid}` profile, `/player/{pid}/image` |
| Teams (×2) | `/team/{tid}`, `/players` (squad), `events/last`, `events/next`, `performance`, season `statistics/overall`, `/image` |
| Referee | `/referee/{rid}`, recent events |
| Tournament | `/tournament`, `/unique-tournament`, season `info`, `standings/total`, round events, logo |
| Media | 58 assets — player photos, team crests (= national flags), tournament logo |

Discovered ids are recorded in `summary.json` (53 players, 2 teams, 2 managers,
referee 789297, venue 2402, tournament 139405, unique-tournament 16, season 58210).

### Expected 404s

27 requests 404 — all legitimate: heatmaps/event-stats for unused substitutes,
group-stage `standings/home` & `standings/away` (only `total` exists for a WC
group), and a few event endpoints that don't exist for a finished match
(`win-probability`, `fan-rating`). They are still recorded in the manifest.

## Output layout

```
event/
  event.json statistics.json incidents.json lineups.json managers.json
  graph.json momentum.json shots.json best_players.json average_positions.json
  h2h.json pregame_form.json team_streaks.json comments.json highlights.json
  summary.json            # discovered ids + status counts
  manifest.jsonl          # one line per request: url, method, status,
                          #   content_type, size, headers, retrieved_at
  raw/                    # EVERY response verbatim + a <stem>.meta.json sidecar
  heatmaps/   {pid}.json
  players/    {pid}_profile.json, {pid}_event_statistics.json
  teams/      {tid}.json, {tid}_players.json, recent/upcoming/performance, ...
  standings/  total.json
  odds/       all.json, featured.json
  predictions/ votes.json, winning_odds.json
  referee/    {rid}.json, {rid}_recent_events.json, referee_from_event.json
  venue/      venue_from_event.json
  tournament/ tournament.json, unique_tournament.json, season_info.json, round_events.json
  media/      player_*.png, team_*.png, manager_*.png, unique_tournament.png
```

## Data integrity

Responses are stored exactly as returned. Each `raw/<stem>.meta.json` holds the
request URL, HTTP status, response headers (auth/cookies stripped), byte size,
content-type and UTC retrieval timestamp. Nothing is transformed or summarised;
the categorised files are byte-identical copies of the raw bodies.

## Scope boundary

Recursion is intentionally bounded to entities **directly attached to this
match** (event, its players/teams/managers/referee/venue and the
tournament/season/standings context). It does not expand each player's entire
club history or every opponent's other fixtures — that would crawl most of
SofaScore. The `summary.json` visited-id set documents exactly what was reached.
