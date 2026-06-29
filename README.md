# get_data

Tooling to build a FIFA World Cup 2026 video-QA dataset: match **videos** plus
the structured **metadata** that annotates them.

## Layout

```
get_data/
  videos/        # match-video downloader (footballorgin.com -> 480p mp4 on /data)
  metadata/      # SofaScore metadata crawler (-> one metadata.json per match)
  AGENTS.md      # agent instructions for the video-download workflow
```

| Dir | What it does | Output |
|-----|--------------|--------|
| [`videos/`](videos/) | Scrapes footballorgin.com, downloads each match variant (1st half / 2nd half / highlights / full match), compresses to 480p H.265. | `/data/1d/simao/football_downloads/downloads/<match_slug>/` |
| [`metadata/`](metadata/) | Crawls the SofaScore API for the same matches and aggregates the useful, video-alignable metadata (events, shots, lineups, ratings, momentum, media). | `metadata/data/<match_slug>/metadata.json` (+ `media/`, `sources/`) |

Both tools name matches with the same `<home>_v_<away>` slug, so a video
directory and its metadata line up by name.

## Data vs code

Only code and small inputs are version-controlled. All generated data is
git-ignored and lives outside the repo or under ignored dirs:

- match videos -> `/data/1d/simao/football_downloads/` (100 TB volume)
- crawled metadata + media -> `metadata/data/` (ignored)
- run logs / reports / progress -> `videos/logs/`, `videos/reports/`,
  `videos/progress.json` (ignored)

See each subdirectory's `README.md` for usage.
