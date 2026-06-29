Crawl SofaScore metadata for all FIFA World Cup 2026 matches.

Source list

`metadata/match_ids.txt` (event_id, slug, status, round) — the authoritative
list of matches, derived from the SofaScore season API (unique-tournament 16,
season 58210). Regenerate it with `python run_crawl_all.py --refresh-ids` when
new fixtures/results appear.

Rules

- Crawl only matches whose status is `finished` (not-yet-played matches have no
  usable metadata).
- Skip matches already done — `metadata/progress.json` AND an existing
  `data/<slug>/metadata.json` are both treated as the source of truth. Do not
  re-crawl them.
- Be polite so the API does not block: keep a per-request delay (default 1.0s)
  and a delay between matches (default 10s). Do not lower these.
- Prune redundant sources after aggregating each match (the runner passes
  `prune_sources=True`).
- Continue after failures; record the error in `progress.json` and move on.
- At the end produce `metadata/reports/report.md`.

How to run

```bash
cd /root/repos/get_data/metadata
python run_crawl_all.py            # crawl all remaining finished matches
```

Optional env knobs: `PER_REQUEST_DELAY`, `MATCH_DELAY`, `LIMIT`, `ONLY`
(slug substring), `INCLUDE_NOTSTARTED=1`.

Finish only after every finished match in `match_ids.txt` has been crawled or
recorded as failed.
