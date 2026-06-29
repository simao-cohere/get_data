# Metadata crawler — agent instructions

This directory crawls **SofaScore** match metadata for FIFA World Cup 2026 and
aggregates each match into a single video-aligned `metadata.json`. It is the
metadata counterpart to the video downloader in `../videos/`.

## Goal

For every finished World Cup match, fetch the useful SofaScore metadata + media,
aggregate it into `data/<home>_v_<away>/metadata.json`, and prune redundant raw
sources — without getting blocked by the API.

## Components

- `crawl.py` — per-match worker. `python crawl.py --event-id <id> --prune-sources`
  writes `data/<slug>/` (metadata.json, media/, sources/, manifest.jsonl,
  summary.json) and auto-aggregates.
- `aggregate.py` — builds `metadata.json` from `sources/`; `--prune-sources`
  deletes the raw files already represented in it.
- `run_crawl_all.py` — resumable batch runner over `match_ids.txt`.
- `match_ids.txt` — event ids + slugs for the whole season (the only tracked
  data file). Regenerate with `python run_crawl_all.py --refresh-ids`.

## Access

The SofaScore API sits behind a Varnish edge that 403s plain clients. `crawl.py`
already handles this with `curl_cffi` browser impersonation + the
`X-Requested-With: XMLHttpRequest` header. No proxy/cookies needed.

## Politeness (important)

Each match is ~115 requests. To avoid being blocked:

- keep `PER_REQUEST_DELAY` ≥ 1.0s (default) and `MATCH_DELAY` ≥ 10s (default);
- run as a single sequential process (no parallel matches);
- never lower the delays to "go faster".

## Tournament keys

FIFA World Cup 2026 = `unique-tournament 16`, `season 58210` (used to enumerate
matches and fetch the group standings).

## Progress / resumability

`progress.json` ({completed, failed, skipped}) is the source of truth, and any
existing `data/<slug>/metadata.json` also counts as done. Re-running never
repeats finished matches. Never stop because one match fails — record it and
continue.

## Storage

Crawled data, logs, reports and progress all stay under this `metadata/`
directory and are git-ignored. Only code, docs, and `match_ids.txt` are tracked.

## Run

```bash
cd /root/repos/get_data/metadata
python run_crawl_all.py
```

or via the Cursor CLI from the repo root:

```bash
cursor-agent "Read metadata/AGENTS.md. Execute metadata/crawl_all.md."
```
