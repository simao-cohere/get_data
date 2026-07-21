#!/usr/bin/env python3
"""Crawler for footballorgin.com match videos.

The site embeds videos through a small chain of pages:

    1. The post page (e.g. ".../portugal-v-uzbekistan-full-match-23-june-2026/")
       exposes a "Multi-Links" list with one entry per video variant
       (1st half / 2nd half / Highlights / Full match ...). Each entry is a
       link of the form  <post-url>?video_index=N  and the post itself has a
       numeric id (found in the og:video:url / shortlink as video_embed=<id>).

    2. The embed page  <post-url>?video_embed=<id>&video_index=N  contains an
       <iframe> that points at the real host, e.g.
            https://soccerfull.net/play/<play_id>

    3. The soccerfull "play" page runs an ArtPlayer with an HLS source, e.g.
            /hls/<play_id>.m3u8   (relative to https://soccerfull.net)

    4. That .m3u8 is a plain VOD playlist whose .ts segments are absolute URLs
       (served from a CDN, sometimes disguised with a .image extension).

This script walks that chain for a chosen variant and downloads the resulting
HLS stream into a single .mp4 using ffmpeg.

Usage examples:

    # List the available variants for a post
    python3 footballorgin_crawler.py <post_url> --list

    # Download the highlights (default)
    python3 footballorgin_crawler.py <post_url>

    # Download a specific variant by label or index
    python3 footballorgin_crawler.py <post_url> --variant "Full match"
    python3 footballorgin_crawler.py <post_url> --index 0

    # Just print the resolved .m3u8 without downloading
    python3 footballorgin_crawler.py <post_url> --variant highlights --resolve-only
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Manual/CLI downloads (as opposed to the batch script, which always passes an
# explicit --out-dir under the large DOWNLOADS volume) default here. Override
# via DOWNLOADS_OUT_DIR or --out-dir if you want a different location. This is
# intentionally NOT a relative "downloads/" folder, since running the CLI from
# a repo checkout could otherwise silently fill up a small repo-hosting volume
# with multi-GB video files.
DEFAULT_OUT_DIR = os.environ.get(
    "DOWNLOADS_OUT_DIR", "/data/1d/simao/football_downloads/manual_downloads"
)


def http_get(url: str, referer: str | None = None, timeout: int = 60) -> str:
    """Fetch a URL and return the decoded body (with retries)."""
    return http_get_bytes_retry(url, referer=referer, timeout=timeout).decode(
        "utf-8", errors="replace"
    )


def http_get_bytes(url: str, referer: str | None = None, timeout: int = 60) -> bytes:
    """Fetch a URL and return the raw bytes."""
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_get_bytes_retry(
    url: str, referer: str | None = None, timeout: int = 60, retries: int = 10
) -> bytes:
    """Fetch raw bytes with retries for transient network errors.

    The video CDN is frequently flaky (sporadic 502 Bad Gateway and truncated
    reads), and a whole multi-thousand-segment download is aborted if a single
    segment gives up, so we retry generously with capped exponential backoff
    plus jitter to ride out transient outages.
    """
    import random

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return http_get_bytes(url, referer=referer, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 (network errors vary widely)
            last_exc = exc
            if attempt < retries:
                wait = min(2 ** attempt, 20) + random.uniform(0, 1.5)
                print(
                    f"    segment fetch failed ({exc}); retry {attempt}/{retries - 1} in {wait:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(wait)
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts: {last_exc}")


@dataclass
class Variant:
    """A single entry from the post's "Multi-Links" list."""

    index: int
    label: str
    page_url: str


def resolve_post_id(post_html: str) -> str:
    """Extract the numeric post id used as the video_embed parameter."""
    for pattern in (
        r"video_embed=(\d+)",
        r"[?&]p=(\d+)",
        r"postid-(\d+)",
        r"post-(\d+)",
    ):
        m = re.search(pattern, post_html)
        if m:
            return m.group(1)
    raise RuntimeError("Could not find the post id (video_embed) on the page.")


def parse_variants(post_html: str) -> list[Variant]:
    """Parse the "Multi-Links" series list into Variant objects."""
    variants: list[Variant] = []
    # Each link looks like:
    #   <a href="...?video_index=2" class="series-item ..." title="Highlights">
    # The first ("1st half") link omits video_index, which means index 0.
    anchor_re = re.compile(
        r'<a\s+href="([^"]+)"\s+class="series-item[^"]*"\s+title="([^"]*)"',
        re.IGNORECASE,
    )
    for href, title in anchor_re.findall(post_html):
        href = href.replace("&#038;", "&").replace("&amp;", "&")
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        index = int(qs.get("video_index", ["0"])[0])
        variants.append(Variant(index=index, label=title.strip(), page_url=href))
    # De-duplicate by index, keeping first occurrence, sorted by index.
    seen: dict[int, Variant] = {}
    for v in variants:
        seen.setdefault(v.index, v)
    return [seen[i] for i in sorted(seen)]


def select_variant(
    variants: list[Variant], variant: str | None, index: int | None
) -> Variant:
    if index is not None:
        for v in variants:
            if v.index == index:
                return v
        raise RuntimeError(f"No variant with index {index}.")
    if variant is not None:
        needle = variant.strip().lower()
        # exact label match first, then substring match.
        for v in variants:
            if v.label.lower() == needle:
                return v
        for v in variants:
            if needle in v.label.lower():
                return v
        raise RuntimeError(f"No variant matching label {variant!r}.")
    # Default: prefer a "highlights" variant, else the first one.
    for v in variants:
        if "highlight" in v.label.lower():
            return v
    return variants[0]


_IFRAME_BLOCKLIST = (
    "googletagmanager",
    "doubleclick",
    "google.com",
    "gstatic",
    "yandex",
    "facebook",
    "/ads",
)


def extract_embed_iframe(embed_html: str) -> str:
    """Find the third-party player iframe URL inside an embed page."""
    # Legacy host used by this site.
    m = re.search(r'https?://soccerfull\.net/play/\d+', embed_html)
    if m:
        return m.group(0)
    for m in re.finditer(r'<iframe[^>]+src="([^"]+)"', embed_html, re.IGNORECASE):
        src = m.group(1).replace("&#038;", "&").replace("&amp;", "&")
        if any(b in src for b in _IFRAME_BLOCKLIST):
            continue
        # Classic /play/ or /embed/ players.
        if re.search(r"/(play|embed)/", src):
            return src
        # Newer embedseek-style single-page players that carry the video id in a
        # URL fragment, e.g. https://matchedcap.embedseek.online/#ivyyh
        if "embedseek" in src or re.search(r"https?://[^/]+/#\w+", src):
            return src
    raise RuntimeError("Could not find a player iframe in the embed page.")


def extract_m3u8(player_html: str, player_url: str) -> str:
    """Extract the HLS playlist URL from a soccerfull-style player page."""
    m = re.search(r'["\']([^"\']+\.m3u8[^"\']*)["\']', player_html)
    if not m:
        # Some players assign the url via a variable like m3u8Url = "...".
        m = re.search(r'm3u8Url\s*=\s*["\']([^"\']+)["\']', player_html)
    if not m:
        raise RuntimeError("Could not find an .m3u8 URL in the player page.")
    return urllib.parse.urljoin(player_url, m.group(1))


# embedseek.online single-page players return AES-128-CBC encrypted JSON from
# their /api/v1/video endpoint. The key/IV are derived client-side purely from
# constants (location.protocol == "https:") so they are static for this deploy.
_EMBEDSEEK_KEY = b"kiemtienmua911ca"
_EMBEDSEEK_IV = b"1234567890oiuytr"


def _embedseek_decrypt(hex_text: str) -> str:
    from Crypto.Cipher import AES  # optional dependency, only for this host

    ciphertext = bytes.fromhex(hex_text.strip())
    plaintext = AES.new(_EMBEDSEEK_KEY, AES.MODE_CBC, _EMBEDSEEK_IV).decrypt(ciphertext)
    pad = plaintext[-1] if plaintext else 0
    if 1 <= pad <= 16:
        plaintext = plaintext[:-pad]
    return plaintext.decode("utf-8", "replace")


def is_embedseek_player(player_url: str) -> bool:
    parsed = urllib.parse.urlparse(player_url)
    return "embedseek" in parsed.netloc or bool(parsed.fragment)


def resolve_embedseek_m3u8(player_url: str) -> tuple[str, str]:
    """Return (m3u8_url, referer_origin) for an embedseek-style player URL."""
    import json as _json

    parsed = urllib.parse.urlparse(player_url)
    video_id = parsed.fragment or urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
    if not video_id:
        raise RuntimeError("embedseek: no video id found in player URL")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    api = f"{origin}/api/v1/video?id={urllib.parse.quote(video_id)}"
    raw = http_get(api, referer=origin + "/")
    data = _json.loads(_embedseek_decrypt(raw))
    src = data.get("source") or data.get("cfNative")
    if not src:
        raise RuntimeError("embedseek: no stream source in decrypted payload")
    return src, origin + "/"


@dataclass
class Resolved:
    variant: Variant
    embed_url: str
    player_url: str
    m3u8_url: str
    referer: str = ""


def resolve(post_url: str, variant: str | None, index: int | None) -> tuple[list[Variant], Resolved]:
    post_html = http_get(post_url)
    post_id = resolve_post_id(post_html)
    variants = parse_variants(post_html)
    if not variants:
        raise RuntimeError("No Multi-Links variants found on the post page.")

    chosen = select_variant(variants, variant, index)

    base = post_url.split("?")[0]
    embed_url = f"{base}?video_embed={post_id}&video_index={chosen.index}"
    embed_html = http_get(embed_url, referer=post_url)
    player_url = extract_embed_iframe(embed_html)

    if is_embedseek_player(player_url):
        m3u8_url, referer = resolve_embedseek_m3u8(player_url)
    else:
        player_html = http_get(player_url, referer=post_url)
        m3u8_url = extract_m3u8(player_html, player_url)
        referer = player_url

    return variants, Resolved(
        variant=chosen,
        embed_url=embed_url,
        player_url=player_url,
        m3u8_url=m3u8_url,
        referer=referer,
    )


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[\s_-]+", "-", text) or "video"


def parse_media_playlist(
    m3u8_text: str, m3u8_url: str, referer: str
) -> tuple[list[str], str | None]:
    """Return ``(segment_urls, init_segment_url)`` for an HLS playlist.

    Handles both master playlists (picks the first variant stream and recurses)
    and plain media playlists. ``init_segment_url`` is the fragmented-MP4
    initialisation segment from ``#EXT-X-MAP`` (``None`` for MPEG-TS streams).
    """
    lines = [ln.strip() for ln in m3u8_text.splitlines() if ln.strip()]

    if any(ln.startswith("#EXT-X-STREAM-INF") for ln in lines):
        # Master playlist: the line after each STREAM-INF is a variant URL.
        for i, ln in enumerate(lines):
            if ln.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
                variant_url = urllib.parse.urljoin(m3u8_url, lines[i + 1])
                sub = http_get(variant_url, referer=referer)
                return parse_media_playlist(sub, variant_url, referer)
        raise RuntimeError("Master playlist had no usable variant stream.")

    init_url: str | None = None
    for ln in lines:
        if ln.startswith("#EXT-X-MAP"):
            m = re.search(r'URI="([^"]+)"', ln)
            if m:
                init_url = urllib.parse.urljoin(m3u8_url, m.group(1))

    segments = [
        urllib.parse.urljoin(m3u8_url, ln)
        for ln in lines
        if not ln.startswith("#")
    ]
    if not segments:
        raise RuntimeError("No media segments found in the playlist.")
    return segments, init_url


def strip_segment_wrapper(data: bytes) -> bytes:
    """Strip any disguise wrapper so the data starts at the MPEG-TS stream.

    Segments on this site are real MPEG-TS payloads prefixed with a tiny PNG
    (signature + IHDR + IEND). MPEG-TS packets are 188 bytes and begin with the
    sync byte 0x47, so we scan for the first offset whose 0x47 sync repeats.
    """
    if data[:1] == b"\x47" and data[188:189] == b"\x47":
        return data  # already raw TS
    limit = min(len(data) - 188 * 3, 8192)
    for off in range(max(limit, 0)):
        if (
            data[off] == 0x47
            and data[off + 188] == 0x47
            and data[off + 376] == 0x47
            and data[off + 564] == 0x47
        ):
            return data[off:]
    return data  # leave untouched if no TS sync detected


def _encode_downscale_cmd(src: str, dst: str, height: int, crf: int) -> list[str]:
    """ffmpeg args to downscale ``src`` to ``height`` (keeps aspect, even dims)."""
    return [
        "ffmpeg", "-y", "-i", src,
        "-vf", f"scale=-2:{height}",
        "-c:v", "libx264",
        "-crf", str(crf),
        "-preset", "veryfast",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        dst,
    ]


def download_hls(
    m3u8_url: str,
    out_path: str,
    referer: str,
    height: int | None = None,
    crf: int = 23,
    two_pass: bool = False,
) -> None:
    """Download an HLS VOD stream and write it to a single mp4.

    Behaviour depending on the options:

    * No ``height``: the stream is remuxed losslessly (stream copy).
    * ``height`` set, ``two_pass=False`` (default): the video is downscaled to
      that height in the SAME single ffmpeg pass (re-encode). No full-res file
      is ever written -- you keep only the downscaled output.
    * ``height`` set, ``two_pass=True``: a full-res mp4 is written first (stream
      copy), then re-encoded/downscaled to ``out_path``, and the full-res
      original is deleted -- so you again keep only the downscaled output, but
      via the explicit two-step path.

    Note: the source here is a single 720p rendition, so downscaling cannot
    reduce the *download* size/time -- it only shrinks the final file on disk.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is not installed or not on PATH.")

    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    playlist = http_get(m3u8_url, referer=referer)
    segments, init_url = parse_media_playlist(playlist, m3u8_url, referer)
    total = len(segments)
    # Fragmented-MP4 streams carry an #EXT-X-MAP init segment and must not have
    # the MPEG-TS "PNG wrapper" stripping applied to them.
    is_fmp4 = init_url is not None
    print(
        f"Downloading {total} segments ({'fMP4' if is_fmp4 else 'MPEG-TS'})...",
        file=sys.stderr,
    )

    fd, ts_path = tempfile.mkstemp(suffix=".mp4" if is_fmp4 else ".ts")
    os.close(fd)
    try:
        with open(ts_path, "wb") as tmp:
            if init_url:
                tmp.write(http_get_bytes_retry(init_url, referer=referer))
            for idx, seg_url in enumerate(segments, 1):
                raw = http_get_bytes_retry(seg_url, referer=referer)
                data = raw if is_fmp4 else strip_segment_wrapper(raw)
                tmp.write(data)
                if idx % 10 == 0 or idx == total:
                    print(f"  {idx}/{total} segments", file=sys.stderr)

        # aac_adtstoasc converts ADTS (MPEG-TS) AAC to the MP4 ASC form; it must
        # not be applied to already-fMP4 audio.
        audio_bsf = [] if is_fmp4 else ["-bsf:a", "aac_adtstoasc"]
        if height and two_pass:
            # Pass 1: write the full-res original (stream copy).
            base, ext = os.path.splitext(out_path)
            original_path = f"{base}.fullres{ext or '.mp4'}"
            copy_cmd = [
                "ffmpeg", "-y", "-i", ts_path,
                "-c", "copy", *audio_bsf,
                "-movflags", "+faststart", original_path,
            ]
            print("Pass 1/2 - remuxing full-res:", " ".join(copy_cmd), file=sys.stderr)
            subprocess.run(copy_cmd, check=True)

            # Pass 2: downscale the original, then delete it.
            scale_cmd = _encode_downscale_cmd(original_path, out_path, height, crf)
            print(f"Pass 2/2 - downscaling to {height}p:", " ".join(scale_cmd), file=sys.stderr)
            subprocess.run(scale_cmd, check=True)

            os.remove(original_path)
            print(f"Deleted full-res original: {original_path}", file=sys.stderr)
        elif height:
            cmd = _encode_downscale_cmd(ts_path, out_path, height, crf)
            print(f"Remuxing + downscaling to {height}p (single pass):", " ".join(cmd), file=sys.stderr)
            subprocess.run(cmd, check=True)
        else:
            cmd = [
                "ffmpeg", "-y", "-i", ts_path,
                "-c", "copy", *audio_bsf,
                "-movflags", "+faststart", out_path,
            ]
            print("Remuxing (stream copy):", " ".join(cmd), file=sys.stderr)
            subprocess.run(cmd, check=True)
    finally:
        try:
            os.remove(ts_path)
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("post_url", nargs="?", help="The footballorgin.com post URL.")
    parser.add_argument(
        "--player-url",
        help=(
            "Bypass the post/embed scrape and download directly from a player "
            "page (e.g. https://soccerfull.net/play/14432) or an .m3u8 URL. "
            "Useful when a variant (e.g. the full match) is not embedded in the "
            "post HTML. Use with -o to name the output."
        ),
    )
    parser.add_argument("--variant", help="Variant label to download (e.g. 'Highlights', 'Full match'). Substring match allowed.")
    parser.add_argument("--index", type=int, help="Variant index to download (overrides --variant).")
    parser.add_argument("--list", action="store_true", help="List available variants and exit.")
    parser.add_argument("--resolve-only", action="store_true", help="Resolve and print the .m3u8 URL without downloading.")
    parser.add_argument("-o", "--output", help="Output .mp4 path (default derived from variant label).")
    parser.add_argument(
        "--out-dir",
        default=DEFAULT_OUT_DIR,
        help=f"Directory for downloads (default: {DEFAULT_OUT_DIR}; override with DOWNLOADS_OUT_DIR env var).",
    )
    parser.add_argument(
        "--height",
        type=int,
        help=(
            "Downscale to this video height in one re-encode pass, e.g. 480. "
            "The source is a single 720p rendition, so this only shrinks the "
            "final file -- it does not reduce download time."
        ),
    )
    parser.add_argument("--crf", type=int, default=23, help="x264 CRF quality when --height is used (lower=better, default 23).")
    parser.add_argument(
        "--two-pass",
        action="store_true",
        help=(
            "With --height: write the full-res mp4 first, re-encode it to the "
            "target height, then delete the full-res original (keep only the "
            "downscaled file). Default is a single pass that never writes the "
            "full-res file."
        ),
    )
    args = parser.parse_args()

    if args.two_pass and not args.height:
        parser.error("--two-pass requires --height (e.g. --height 480).")

    if not args.post_url and not args.player_url:
        parser.error("Provide a post URL, or use --player-url for a direct source.")

    if args.player_url:
        player_url = args.player_url
        if re.search(r"\.m3u8(\?|$)", player_url):
            m3u8_url = player_url
        else:
            player_html = http_get(player_url, referer=args.post_url or player_url)
            m3u8_url = extract_m3u8(player_html, player_url)

        print(f"Player:    {player_url}", file=sys.stderr)
        print(f"M3U8:      {m3u8_url}", file=sys.stderr)
        if args.resolve_only:
            print(m3u8_url)
            return 0

        out_path = args.output
        if not out_path:
            stem = slugify(args.variant) if args.variant else "video"
            out_path = os.path.join(args.out_dir, f"{stem}.mp4")
        try:
            download_hls(
                m3u8_url,
                out_path,
                referer=player_url,
                height=args.height,
                crf=args.crf,
                two_pass=args.two_pass,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"Download failed: {exc}", file=sys.stderr)
            return 1
        print(f"\nSaved: {out_path}")
        return 0

    if args.list:
        post_html = http_get(args.post_url)
        variants = parse_variants(post_html)
        if not variants:
            print("No variants found.", file=sys.stderr)
            return 1
        print(f"Variants for {args.post_url}:")
        for v in variants:
            print(f"  [{v.index}] {v.label}")
        return 0

    try:
        variants, res = resolve(args.post_url, args.variant, args.index)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print("Available variants:", ", ".join(f"[{v.index}] {v.label}" for v in variants), file=sys.stderr)
    print(f"Selected:  [{res.variant.index}] {res.variant.label}", file=sys.stderr)
    print(f"Embed URL: {res.embed_url}", file=sys.stderr)
    print(f"Player:    {res.player_url}", file=sys.stderr)
    print(f"M3U8:      {res.m3u8_url}", file=sys.stderr)

    if args.resolve_only:
        print(res.m3u8_url)
        return 0

    out_path = args.output
    if not out_path:
        out_path = os.path.join(args.out_dir, f"{slugify(res.variant.label)}.mp4")

    try:
        download_hls(
            res.m3u8_url,
            out_path,
            referer=res.player_url,
            height=args.height,
            crf=args.crf,
            two_pass=args.two_pass,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Download failed: {exc}", file=sys.stderr)
        return 1

    print(f"\nSaved: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
