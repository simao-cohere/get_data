# videos — match-video downloader

Crawler for downloading match videos from footballorgin.com.

## How it works

footballorgin.com serves each match as a WordPress post that exposes several
video variants (1st half / 2nd half / Highlights / Full match) through a
"Multi-Links" list. The real stream is reached via a short chain of pages:

1. **Post page** — e.g.
   `https://www.footballorgin.com/portugal-v-uzbekistan-full-match-23-june-2026/`
   The post has a numeric id (exposed as `video_embed=<id>`), and the
   Multi-Links list gives one `?video_index=N` link per variant.
2. **Embed page** — `<post-url>?video_embed=<id>&video_index=N` contains an
   `<iframe>` pointing at the real host, e.g. `https://soccerfull.net/play/<id>`.
3. **Player page** — `soccerfull.net/play/<id>` runs ArtPlayer with an HLS
   source at `/hls/<id>.m3u8`.
4. **HLS playlist** — a plain VOD `.m3u8` whose `.ts` segments are absolute CDN
   URLs. The segments are real MPEG-TS payloads **disguised behind a tiny PNG
   header** (signature + IHDR + IEND) to look like images. The crawler strips
   that wrapper before concatenating the segments, then remuxes them into a
   single `.mp4` with `ffmpeg`.

## Requirements

- Python 3.10+ (standard library only)
- `ffmpeg` on your `PATH` (used only for the final local remux)

## Batch download (all remaining World Cup matches)

The batch script `run_download_20.py` walks `world_cup_matches.txt`, skips
anything already in `progress.json`, downloads every available variant at 720p,
compresses to 480p H.265, and saves videos under
`/data/1d/simao/football_downloads/downloads/`. Logs, reports and
`progress.json` are written next to the script (in this `videos/` directory).

Run it via the Cursor CLI from the repo root — it reads `AGENTS.md` for
storage/config context and `videos/download_next_20.md` for the task:

```bash
cd /root/repos/get_data
cursor-agent "Read AGENTS.md. Execute videos/download_next_20.md."
```

Or run it directly in a terminal (survives terminal close with `nohup`):

```bash
cd /root/repos/get_data/videos
nohup env TARGET_SUCCESSES=100 python run_download_20.py > logs/run_remaining.log 2>&1 &
tail -f logs/run_remaining.log   # follow progress
```

`progress.json` is the source of truth — re-running is safe and will never
re-download a match already marked completed or skipped.

## Single-match usage

```bash
POST_URL="https://www.footballorgin.com/portugal-v-uzbekistan-full-match-23-june-2026/"

# List the available variants
python3 footballorgin_crawler.py "$POST_URL" --list

# Download the highlights (default when no variant is given)
python3 footballorgin_crawler.py "$POST_URL" --variant highlights

# Download a specific variant by label or index
python3 footballorgin_crawler.py "$POST_URL" --variant "Full match"
python3 footballorgin_crawler.py "$POST_URL" --index 0

# Resolve and print the .m3u8 URL without downloading
python3 footballorgin_crawler.py "$POST_URL" --variant highlights --resolve-only

# Downscale to 480p in a single re-encode pass (smaller file on disk)
python3 footballorgin_crawler.py "$POST_URL" --variant highlights --height 480

# Explicit two-pass: write full-res, re-encode to 480p, delete the original
python3 footballorgin_crawler.py "$POST_URL" --variant highlights --height 480 --two-pass

# Download directly from a player page / m3u8 (bypasses the post/embed scrape),
# e.g. the full match, which is NOT embedded in the post HTML:
python3 footballorgin_crawler.py --player-url "https://soccerfull.net/play/14432" \
    --height 480 -o downloads/full_match_480p.mp4
```

Downloads are written to `downloads/<variant>.mp4` by default; override with
`-o/--output` or change the directory with `--out-dir`.

## Resolution / downscaling

The host serves a **single 720p rendition** (the `.m3u8` is a plain media
playlist with no multi-resolution `#EXT-X-STREAM-INF` variants). Consequences:

- You **cannot download a lower resolution directly** — there is no 480p stream
  to fetch. Downscaling therefore **does not speed up the download**; the
  network cost is the same ~720p data either way.
- `--height N` re-encodes (downscales) the assembled stream in the **same single
  ffmpeg pass**, so there's no intermediate 720p file. This is more efficient
  than downloading at 720p and then running a separate
  `ffmpeg -i in.mp4 -vf scale=854:480 out.mp4` (which is a second full pass +
  extra disk). Re-encoding is CPU-bound; tune quality with `--crf` (default 23).
- `--two-pass` (requires `--height`) does the explicit two-step flow: write the
  full-res mp4 (`<name>.fullres.mp4`), re-encode it to the target height, then
  **delete the full-res original**, leaving only the downscaled file. The
  default single pass already produces only the downscaled file (without ever
  writing the full-res one), so prefer it unless you specifically want a clean
  full-res intermediate to exist during encoding.

Either way (`--height` alone or `--height --two-pass`) you end up with **only
the downscaled file** -- ideal for ingesting just the 480p output downstream.

For reference, the highlights clip is ~216 MB at 720p (stream copy) and ~137 MB
at 480p (`--height 480`).

## Example variant mapping

For the Portugal v Uzbekistan post:

| index | label       | soccerfull id |
|-------|-------------|---------------|
| 0     | 1st half    | 14430         |
| 1     | 2nd half    | 14431         |
| 2     | Highlights  | 14433         |
| 3     | Highllights | 14433 (dup)   |
| 4     | Full match  | 14432 (not embedded; use `--player-url`) |

> Note: the "Full match" variant's iframe is not present in the server-rendered
> embed HTML, so indices 0–3 resolve automatically while the full match must be
> fetched with `--player-url https://soccerfull.net/play/14432` (its id sits in
> the gap between the halves and the highlights; its ~123 min runtime matches
> both halves combined). Use `--player-url` whenever a variant isn't embedded.
