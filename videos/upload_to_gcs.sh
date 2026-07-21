#!/bin/bash
# Upload downloaded match videos (+ metadata) to GCS.
#
# Replaces the older one-off scripts (upload_new_to_gcs.sh, upload_remaining.sh,
# upload_extratime_fix.sh) with a single parametrized tool. The match list is an
# input (positional args or --from-file), and the normalisation rules that used
# to differ between those scripts are applied uniformly:
#   - skip full_match_* when both 1st and 2nd half compressed files exist
#     ("split halves beat full match")
#   - normalise the extra-time variant filename to the canonical form
#   - normalise highlight/highllights -> highlights
#
# Usage:
#   ./upload_to_gcs.sh MATCH_DIR [MATCH_DIR ...]
#   ./upload_to_gcs.sh --from-file matches.txt
#   ./upload_to_gcs.sh --only et --no-metadata argentina_v_switzerland
#   ./upload_to_gcs.sh --dry-run france_v_senegal
#
# MATCH_DIR is the on-disk directory name (e.g. france_v_senegal), matching the
# layout under $DL/<match>/compressed and $META/<match>.
#
# Paths/bucket are overridable via environment variables:
#   BASE   GCS destination prefix
#   DL     local downloads root (contains <match>/compressed/*.mp4)
#   META   local metadata root  (contains <match>/*.json)
set -u

BASE="${BASE:-gs://cohere-dev-central-2/video-intelligence/data_cache_raw/hackathon_2026}"
DL="${DL:-/data/1d/simao/football_downloads/downloads}"
META="${META:-/root/repos/get_data/metadata/data}"
CANON_ET="extra_time_and_penalties_if_any_480p30_h265.mp4"
CANON_HL="highlights_480p30_h265.mp4"

only=""          # "" = all variants; "et" = extra-time variant only
do_metadata=1
dry_run=0
matches=()

usage() {
  sed -n '2,32p' "$0"
  exit "${1:-0}"
}

# Only upload files/dirs whose names we generated ourselves. Reject anything
# else so a stray argument can't be turned into an arbitrary gsutil path.
valid_match() {
  [[ "$1" =~ ^[a-zA-Z0-9_-]+$ ]]
}

while [ $# -gt 0 ]; do
  case "$1" in
    --from-file)
      [ $# -ge 2 ] || { echo "!! --from-file needs a path" >&2; exit 2; }
      [ -f "$2" ] || { echo "!! no such file: $2" >&2; exit 2; }
      while IFS= read -r line || [ -n "$line" ]; do
        line="${line%%#*}"                 # strip comments
        line="$(echo "$line" | xargs)"     # trim whitespace
        [ -n "$line" ] && matches+=("$line")
      done < "$2"
      shift 2
      ;;
    --only)
      [ $# -ge 2 ] || { echo "!! --only needs a value (e.g. et)" >&2; exit 2; }
      only="$2"; shift 2
      ;;
    --no-metadata) do_metadata=0; shift ;;
    --dry-run)     dry_run=1; shift ;;
    -h|--help)     usage 0 ;;
    --) shift; while [ $# -gt 0 ]; do matches+=("$1"); shift; done ;;
    -*) echo "!! unknown option: $1" >&2; usage 2 ;;
    *)  matches+=("$1"); shift ;;
  esac
done

[ "${#matches[@]}" -gt 0 ] || { echo "!! no matches given" >&2; usage 2; }

# Copy $1 -> $2 (honours --dry-run). Returns gsutil's exit status.
gcs_cp() {
  if [ "$dry_run" = 1 ]; then
    echo "  [dry-run] gsutil cp '$1' '$2'"
    return 0
  fi
  gsutil cp "$1" "$2"
}

upload_videos() {
  local d="$1" cdir="$DL/$1/compressed"
  if ! compgen -G "$cdir/*.mp4" > /dev/null; then
    echo "  !! no local compressed videos for $d"
    return
  fi

  local have_h1=0 have_h2=0
  [ -e "$cdir/1st_half_480p30_h265.mp4" ] && have_h1=1
  [ -e "$cdir/2nd_half_480p30_h265.mp4" ] && have_h2=1

  local f base dest kind
  for f in "$cdir"/*.mp4; do
    [ -e "$f" ] || continue
    base="$(basename "$f")"

    # Classify + normalise destination filename.
    case "$base" in
      et_amp_pen_if_any_*|extra_time_and_penalt*) kind="et";         dest="$CANON_ET" ;;
      highlight_480p30_h265.mp4|highllights_480p30_h265.mp4) kind="highlights"; dest="$CANON_HL" ;;
      full_match_*) kind="full_match"; dest="$base" ;;
      1st_half_*)   kind="first_half"; dest="$base" ;;
      2nd_half_*)   kind="second_half"; dest="$base" ;;
      *)            kind="other";      dest="$base" ;;
    esac

    # --only filter (e.g. only re-upload the extra-time variant).
    if [ -n "$only" ] && [ "$kind" != "$only" ]; then
      continue
    fi

    # Split halves beat full match.
    if [ "$kind" = "full_match" ] && [ "$have_h1" = 1 ] && [ "$have_h2" = 1 ]; then
      echo "  [skip] $base (both halves present)"
      continue
    fi

    echo "  [video] $base -> $dest"
    gcs_cp "$f" "$BASE/$d/videos/$dest" || echo "  !! VIDEO FAILED $d/$base"
  done
}

upload_metadata() {
  local d="$1" mdir="$META/$1"
  if compgen -G "$mdir/*.json" > /dev/null; then
    echo "  [metadata] $(ls "$mdir"/*.json | wc -l) file(s)"
    if [ "$dry_run" = 1 ]; then
      echo "  [dry-run] gsutil -m cp '$mdir'/*.json '$BASE/$d/metadata/'"
    else
      gsutil -m cp "$mdir"/*.json "$BASE/$d/metadata/" || echo "  !! META FAILED $d"
    fi
  else
    echo "  !! no local metadata for $d"
  fi
}

for d in "${matches[@]}"; do
  if ! valid_match "$d"; then
    echo "!! skipping invalid match name: '$d'" >&2
    continue
  fi
  echo "======== $d ========"
  upload_videos "$d"
  [ "$do_metadata" = 1 ] && upload_metadata "$d"
done
echo "ALL_UPLOADS_DONE"
