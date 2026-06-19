# vimeo-playlist-download

Download a Vimeo video from its `playlist.json` manifest URL. Reconstructs the
segmented DASH streams (video + audio), then muxes them into a single MP4 with
ffmpeg.

Useful for videos you have legitimate access to but that aren't exposed through
the normal `vimeo.com/<id>` page (e.g. private review links), where standard
tools fall back to a generic extractor and fail.

## Requirements

- Python 3.8+ (standard library only — no `pip install` needed)
- [ffmpeg](https://ffmpeg.org/) on your `PATH`

Works on macOS, Linux, and Windows.

## Usage

```bash
python3 vimeo_dl.py [-j N] output.mp4 "MANIFEST_URL"
```

- `-j N` — number of concurrent download workers (default 8). The audio phase in
  particular is latency-bound (many tiny segments), so concurrency helps a lot.
  Try 8–16; too high can trigger throttling.
- `output.mp4` — output filename (first positional arg).
- `MANIFEST_URL` — the `playlist.json` URL (always the **last** arg). See below.

Example:

```bash
python3 vimeo_dl.py -j 8 lecture.mp4 "https://...vimeocdn.com/.../playlist.json?..."
```

## Getting the manifest URL (the one non-obvious step)

The script needs the exact `playlist.json` URL the Vimeo player fetched. Get it
from your browser:

1. Open the page where the video plays.
2. Open DevTools → **Network** tab.
3. In the filter box, type `playlist`.
4. **Play the video.** A request to `.../v2/playlist/.../playlist.json?...` appears.
5. Right-click it → **Copy** → **Copy URL**.
6. Paste that as the last argument to the script, in quotes.

> **The URL expires.** It carries a signed, time-limited token. Grab a fresh URL
> right before running. If you get a `403`, the signature lapsed — copy a new URL
> and re-run (completed segments resume automatically).

## Features

- **Concurrent downloads** with strict in-order writing (no corruption).
- **Resume**: if interrupted, re-run the same command and it continues from the
  last completed segment (per-track `.progress` sidecar).
- **Retries** on timeouts and transient network errors.
- Picks the **highest-resolution** video track and the **primary** audio track
  automatically.
- Parallel-safe: temp files are derived from the output name, so multiple
  instances with different output names won't collide.

## Notes

- Output size = duration × bitrate. A multi-hour 1080p video can be several GB.
  To shrink afterward (slow, lossy):
  ```bash
  ffmpeg -i output.mp4 -c:v libx265 -crf 28 -preset medium -c:a copy smaller.mp4
  ```

## Disclaimer

This tool is for downloading content you have the legal right to access and save.
Downloading content without permission may violate Vimeo's Terms of Service and
applicable copyright law. You are responsible for how you use it.

## License

MIT
