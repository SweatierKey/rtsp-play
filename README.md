# rtsp-play

Open an RTSP URL in a local viewer. A wrapper that prefers
[`mpv`](https://mpv.io) and falls back to `ffplay`. All player flags are
tuned for low-latency live playback.

## Demo

![demo](demo.gif)

Watch with pause/seek on [asciinema.org](https://asciinema.org/a/9TbiVLGqXQcm3dz0).

## Install

    chmod +x rtsp-play
    cp rtsp-play ~/.local/bin/    # or /usr/local/bin/

You also need at least one of:

    apt install mpv         # preferred
    apt install ffmpeg      # provides ffplay

## Usage

    rtsp-play rtsp://192.168.1.64/live/ch00_1

Pipe straight from the rest of the chain:

    onvif-rtsp --user admin --password segreta \
        http://192.168.1.64/onvif/device_service \
      | rtsp-play

Force ffplay even if mpv is installed, drop audio, switch transport to UDP:

    rtsp-play --player ffplay --no-audio --transport udp rtsp://192.168.1.64/live/ch00_1

### Flags

| Flag | Default | Meaning |
|---|---|---|
| `RTSP_URL` (positional) | from stdin | RTSP URL (`rtsp://` or `rtsps://`) |
| `--player {auto,mpv,ffplay}` | `auto` | which player to use; `auto` picks `mpv` when present, else `ffplay` |
| `--transport {tcp,udp}` | `tcp` | RTSP transport; TCP is more reliable on most LANs |
| `--no-audio` | off | disable audio |
| `-v`, `--verbose` | off | let the player log on stderr (default: silent) |
| `-V`, `--version` | | print version and exit |
| `-h`, `--help` | | show help and exit |

Stdin handling matches `onvif-rtsp`: if `RTSP_URL` is omitted, the first
non-empty line of stdin is used.

### Low-latency tuning

The arguments passed to each player are tuned for live RTSP, not file playback:

- **mpv** — `--profile=low-latency` (sets `cache=no`, `vd-lavc-threads=1`,
  `video-latency-hacks=yes`, `interpolation=no`, demuxer `+nobuffer`, ...) plus
  `--demuxer-lavf-probesize=32`, `--demuxer-lavf-analyzeduration=0`,
  `--cache=no`, `--framedrop=decoder+vo`, `+discardcorrupt`. The RTSP transport
  is layered with `--demuxer-lavf-o-*add*` so the profile defaults are not
  clobbered.
- **ffplay** — `-fflags nobuffer+discardcorrupt`, `-flags low_delay`,
  `-probesize 32`, `-analyzeduration 0`, `-framedrop`, `-rtsp_transport <t>`.

In non-verbose mode the player's stderr is sent to `/dev/null` (mpv and ffplay
are both chatty by default). With `-v`, it passes through unchanged.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | the player exited cleanly |
| 1 | usage error (missing/bad URL, neither mpv nor ffplay in PATH, requested player not in PATH) |
| any other | the player's own exit status, propagated unchanged |
| 130 | interrupted with Ctrl-C |

## Dependencies

- Python 3.8+ (stdlib only)
- One of `mpv` (preferred) or `ffplay` from the `ffmpeg` package

## Place in the chain

    onvif-discover → onvif-rtsp → go2rtc-gen → rtsp-play / rtsp-record → footage-merge

`rtsp-play` is a terminal node — it consumes a single RTSP URL and produces no
data on stdout. Its sibling for unattended recording is `rtsp-record`.
