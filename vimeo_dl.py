#!/usr/bin/env python3
"""
Download a Vimeo segmented DASH-JSON video from its playlist.json manifest URL.

Usage:
    python3 vimeo_dl.py [-j N] output.mp4 "MANIFEST_URL"
  (or, to reuse a manifest you already saved:)
    python3 vimeo_dl.py [-j N] output.mp4 saved_playlist.json "MANIFEST_URL"

- -j N sets concurrent download workers (default 8). Higher = faster up to your
  bandwidth / Vimeo's per-IP cap; try 8-16. Too high can trigger throttling.
- MANIFEST_URL (always the LAST arg) is the exact .../playlist.json?... URL you
  grabbed from the browser network tab. The script fetches it itself. Relative
  segment paths resolve against this URL.
- Temp files are derived from the output name, so multiple instances with
  different output names will NOT collide.
- Signatures inside the manifest EXPIRE (sometimes within minutes). Grab a
  fresh URL right before running.

Requires: ffmpeg on PATH. Uses only the Python stdlib.
"""
import sys
import os
import json
import base64
import time
import subprocess
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:147.0) "
                   "Gecko/20100101 Firefox/147.0"),
    "Referer": "https://player.vimeo.com/",
    "Origin": "https://player.vimeo.com",
    "Accept": "*/*",
}


def fetch(url, retries=6):
    """Fetch bytes with retries. Retries on timeouts, socket errors, 5xx, and
    transient network failures. Bails immediately only on 403 (expired sig)."""
    last = None
    for attempt in range(retries):
        try:
            req = Request(url, headers=HEADERS)
            with urlopen(req, timeout=60) as r:
                return r.read()
        except HTTPError as e:
            last = e
            if e.code == 403:
                raise SystemExit(
                    "\n403 Forbidden on a segment. The manifest signatures have "
                    "expired.\nGrab a FRESH playlist.json URL and re-run "
                    "(completed segments are resumed automatically).")
            # 5xx and other HTTP errors: retry with backoff
        except (URLError, TimeoutError, OSError) as e:
            # includes socket timeouts, connection resets, SSL read timeouts
            last = e
        time.sleep(min(2 ** attempt, 15))
    raise SystemExit(f"Failed to fetch after {retries} tries: {url}\n{last}")


def pick_video(tracks):
    # highest resolution, then highest bitrate
    return max(tracks, key=lambda t: (t.get("height", 0), t.get("bitrate", 0)))


def pick_audio(tracks):
    primary = [t for t in tracks if t.get("audio_primary")]
    pool = primary or tracks
    return max(pool, key=lambda t: t.get("bitrate", 0))


def download_track(track, manifest_url, manifest_base, out_path, label,
                   workers=4):
    # Resolve the directory each segment URL is relative to:
    #   manifest_url joined with the manifest-level base_url, then the track base_url
    base = urljoin(manifest_url, manifest_base)
    base = urljoin(base, track.get("base_url", ""))

    segs = track["segments"]
    total = len(segs)
    seg_urls = [urljoin(base, s["url"]) for s in segs]

    # --- Resume support ---------------------------------------------------
    # Sidecar records (segments_written, byte_offset) of the last clean write.
    # On restart we truncate the part file back to that offset and continue.
    sidecar = out_path + ".progress"
    start_index = 0
    if os.path.exists(out_path) and os.path.exists(sidecar):
        try:
            with open(sidecar) as sf:
                done_n, offset = (int(x) for x in sf.read().split())
            real = os.path.getsize(out_path)
            if real >= offset and 0 < done_n <= total:
                # trim any partial trailing write, then resume
                with open(out_path, "r+b") as tf:
                    tf.truncate(offset)
                start_index = done_n
                print(f"[{label}] resuming at segment {start_index}/{total} "
                      f"({offset/1e6:.1f} MB already on disk)")
        except Exception as e:
            print(f"[{label}] could not read resume state ({e}); restarting track")
            start_index = 0

    print(f"[{label}] {track.get('width','')}x{track.get('height','')} "
          f"codec={track.get('codecs')} bitrate={track.get('bitrate')} "
          f"-> {total} segments, {workers} workers")

    # Open append if resuming, else fresh (and write the init segment first).
    mode = "r+b" if start_index > 0 else "wb"
    with open(out_path, mode) as f:
        if start_index > 0:
            f.seek(0, os.SEEK_END)
            done_bytes = f.tell()
        else:
            init_b64 = track.get("init_segment")
            if init_b64:
                f.write(base64.b64decode(init_b64))
            done_bytes = f.tell()

        t0 = time.time()
        bytes_at_start = done_bytes
        with ThreadPoolExecutor(max_workers=workers) as ex:
            pending = {}                  # index -> future
            next_to_submit = start_index
            next_to_write = start_index
            # prime the pipeline
            while next_to_submit < total and len(pending) < workers * 2:
                pending[next_to_submit] = ex.submit(fetch, seg_urls[next_to_submit])
                next_to_submit += 1

            while next_to_write < total:
                fut = pending.pop(next_to_write)
                data = fut.result()       # blocks only on the one we need next
                f.write(data)
                done_bytes += len(data)
                idx = next_to_write
                next_to_write += 1
                # record clean-write checkpoint (flush so it's durable)
                f.flush()
                with open(sidecar, "w") as sf:
                    sf.write(f"{next_to_write} {done_bytes}")
                # top up the pipeline
                if next_to_submit < total:
                    pending[next_to_submit] = ex.submit(fetch, seg_urls[next_to_submit])
                    next_to_submit += 1

                if (idx + 1) % 40 == 0 or (idx + 1) == total:
                    elapsed = time.time() - t0
                    sess_mb = (done_bytes - bytes_at_start) / 1e6
                    rate = sess_mb / elapsed if elapsed else 0
                    pct = 100 * (idx + 1) / total
                    print(f"  [{label}] {idx+1}/{total} ({pct:5.1f}%)  "
                          f"{done_bytes/1e6:8.1f} MB  {rate:5.1f} MB/s", flush=True)

    # track complete -> remove sidecar
    if os.path.exists(sidecar):
        os.remove(sidecar)

    print(f"[{label}] done -> {out_path} ({done_bytes/1e6:.1f} MB)")


def main():
    args = sys.argv[1:]

    # Optional -j N (workers), anywhere before the positional args.
    workers = 8
    if "-j" in args:
        i = args.index("-j")
        try:
            workers = int(args[i + 1])
        except (IndexError, ValueError):
            print("Error: -j requires an integer, e.g. -j 12")
            sys.exit(1)
        del args[i:i + 2]

    if len(args) not in (2, 3):
        print(__doc__)
        sys.exit(1)
    # URL is always last; output is always first.
    out_mp4 = args[0]
    manifest_url = args[-1]
    manifest_path = args[1] if len(args) == 3 else None

    if manifest_path:
        print(f"Reading manifest from file: {manifest_path}")
        with open(manifest_path) as f:
            m = json.load(f)
    else:
        print("Fetching manifest from URL...")
        m = json.loads(fetch(manifest_url).decode("utf-8"))

    manifest_base = m.get("base_url", "")
    v = pick_video(m["video"])
    a = pick_audio(m["audio"])

    # Derive temp names from the output so parallel runs don't collide.
    stem = os.path.splitext(out_mp4)[0]
    video_raw = f"{stem}._video.mp4"
    audio_raw = f"{stem}._audio.mp4"

    download_track(v, manifest_url, manifest_base, video_raw, "video", workers)
    download_track(a, manifest_url, manifest_base, audio_raw, "audio", workers)

    print("Muxing with ffmpeg...")
    try:
        result = subprocess.run([
            "ffmpeg", "-y",
            "-i", video_raw,
            "-i", audio_raw,
            "-c", "copy",
            "-movflags", "+faststart",
            out_mp4,
        ])
    except FileNotFoundError:
        print("ffmpeg not found on PATH. Install it, then mux manually:\n"
              f'  ffmpeg -i "{video_raw}" -i "{audio_raw}" -c copy "{out_mp4}"')
        sys.exit(1)
    if result.returncode != 0:
        print("ffmpeg failed. Raw streams kept:", video_raw, audio_raw)
        sys.exit(1)

    os.remove(video_raw)
    os.remove(audio_raw)
    print(f"\nDone -> {out_mp4}")


if __name__ == "__main__":
    main()
