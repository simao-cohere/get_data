#!/usr/bin/env bash
# Download available variants (1st half / 2nd half / highlights) at 480p for a
# set of footballorgin.com matches, organise them into per-match directories,
# and upload each directory to GCS under hackathon_2026/<match_dir>/.
#
# "Full match" entries are not embedded server-side on these posts (only the
# halves + highlights are), so the two halves provide full coverage.
set -uo pipefail

cd "$(dirname "$0")"

DEST_ROOT="gs://cohere-dev-central-2/video-intelligence/data_cache_raw/hackathon_2026"
CRAWLER="python3 footballorgin_crawler.py"

# match_dir|post_url
MATCHES=(
  "colombia_v_dr_congo|https://www.footballorgin.com/colombia-v-dr-congo-full-match-23-june-2026/"
  "panama_v_croatia|https://www.footballorgin.com/panama-v-croatia-full-match-23-june-2026/"
  "england_v_ghana|https://www.footballorgin.com/england-v-ghana-full-match-23-june-2026/"
)

# variant label|output filename
VARIANTS=(
  "1st half|1st_half.mp4"
  "2nd half|2nd_half.mp4"
  "Highlights|highlights.mp4"
)

for entry in "${MATCHES[@]}"; do
  dir="${entry%%|*}"
  url="${entry##*|}"
  outdir="downloads/$dir"
  mkdir -p "$outdir"
  echo "############################################################"
  echo "# MATCH: $dir"
  echo "# URL:   $url"
  echo "############################################################"

  for v in "${VARIANTS[@]}"; do
    label="${v%%|*}"
    fname="${v##*|}"
    out="$outdir/$fname"
    if [ -f "$out" ]; then
      echo ">> [$dir] $label already exists ($out), skipping."
      continue
    fi
    echo ">> [$dir] downloading '$label' -> $out"
    if $CRAWLER "$url" --variant "$label" --height 480 -o "$out" 2>&1 \
         | grep -viE "kjobs|Config file|--job" \
         | grep -iE "selected|m3u8:|segments$|downscal|saved|error|failed"; then
      :
    fi
    if [ -f "$out" ]; then
      sz=$(du -h "$out" | cut -f1)
      echo ">> [$dir] OK $label ($sz)"
    else
      echo ">> [$dir] MISSING $label (not available / failed)"
    fi
    sleep 3
  done

  echo ">> [$dir] uploading to $DEST_ROOT/$dir/"
  gsutil -m cp "$outdir"/*.mp4 "$DEST_ROOT/$dir/" 2>&1 \
    | grep -viE "kjobs|Config file|--job" | grep -iE "copying|operation|error" || true
done

echo "============================================================"
echo "DONE. Final bucket listing:"
gsutil ls -lh "$DEST_ROOT/**" 2>&1 | grep -viE "kjobs|Config file|--job"
echo "ALL_MATCHES_COMPLETE"
