# Project

This repository downloads and processes FIFA World Cup 2026 match videos from
footballorgin.com. The downloader and its task spec live in `videos/`
(`videos/run_download_20.py`, `videos/footballorgin_crawler.py`,
`videos/download_next_20.md`, `videos/world_cup_matches.txt`). Match metadata
tooling lives separately in `metadata/`.

## Goal

Download match videos while preserving quality and producing compressed versions.

## Workflow

For each match:

1. Find the match page.
2. Locate every downloadable video.
3. Download the highest quality version.
4. Verify download integrity.
5. Produce a compressed copy:
   - 30 FPS
   - 480p
   - target bitrate ≈60% of original
6. Verify compressed file.
7. Update progress.
8. Continue.

Never stop because one match fails.

Skip unavailable matches.

Retry temporary failures.

## Storage

Large video files must NOT go under `/root/repos` — that volume is 1.1 TB and
nearly full (~94%). Use `/data/1d/simao/football_downloads/` instead, which
sits on the 100 TB `/data` volume (79 TB free).

```
/data/1d/simao/football_downloads/
    downloads/          # original + compressed videos, organised by match slug
    progress/           # progress.json
    reports/            # report.md
    logs/               # run.log
```

The `DOWNLOADS` root in `videos/run_download_20.py` (and the `--out-dir` flag in
`videos/footballorgin_crawler.py`) should therefore be set to
`/data/1d/simao/football_downloads/downloads`.

## Output layout

```
/data/1d/simao/football_downloads/downloads/
    <match_slug>/
        <variant>.mp4                              # original (~720p)
        compressed/<variant>_480p30_h265.mp4       # compressed copy
    incomplete_<match_slug>/                       # dirs with no videos found
```

Logs, reports, and progress files stay next to the downloader in `videos/`
(they are small) and are git-ignored:

```
/root/repos/get_data/videos/
    progress.json
    progress/
    reports/report.md
    logs/run.log
```

## Compression

ffmpeg

30 FPS

480p

bitrate ≈60% of source

## Progress

Maintain:

`/root/repos/get_data/videos/progress.json` (and a copy in `videos/progress/progress.json`)

Each completed match should include:

- match
- original path
- compressed path
- status
- errors (if any)

## Reporting

At the end generate

reports/report.md

Include

- successful downloads
- skipped matches
- failures
- total size
- total runtime

## Robustness

Never ask for confirmation.

If one match fails:

- retry
- if still failing
- record
- continue.

Continue until task completion.

## Autonomous Execution

Operate autonomously.

Do not pause waiting for user input.

If multiple reasonable choices exist:

Choose the option that maximizes successful downloads.

If blocked on one match:

Skip it.

Continue.

At completion produce:

reports/report.md

and exit normally.