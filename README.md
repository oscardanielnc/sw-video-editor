# SW Video Editor

A local Windows video editor built for one job the mainstream tools are bad at:
**fast, surgical edits on very large recordings (10–20 GB)**. Trim, delete
sections, reorder, rotate, export. Few features, no waiting.

The interesting part is not the feature list. It is that **no operation ever
reads the whole file** — importing a 22 MB clip and a 1.3 GB clip cost the same
44 ms — and that the export path is built on a format-level constraint that had
to be measured before it could be designed around.

```
run.bat
```

Or drag one or more videos onto `run.bat`.

---

## Engineering highlights

| | |
|---|---|
| **Flat cost vs. file size** | Import, seek and split are O(1) in file size. Measured: 38 ms vs. 44 ms for 22 MB vs. 1.3 GB. |
| **A design driven by measurement** | The obvious "smart render" design produces corrupt video. Proven with 6 experiments across 4 container formats before the architecture was changed. |
| **Correctness verified, not assumed** | Exports are decoded and checked frame-by-frame for freeze artifacts, by hash — not by eye. |
| **Security audited against a written threat model** | 11 findings, all fixed, all regression-tested. See [SECURITY.md](SECURITY.md). |
| **144 automated checks** | 96 functional + 48 security, in one command. |
| **Zero runtime dependencies beyond two libraries** | PySide6 and python-mpv. No framework, no ORM, no build step. |

---

## Why it doesn't choke on 20 GB

No operation decodes the file. Measured on a 1 h / 1.3 GB file vs. a 22 MB one
(`tests/bench_large.py`):

| Operation | 22 MB | 1.3 GB |
|---|---|---|
| Import | 38 ms | 44 ms |
| Find keyframes (at 51 min) | 54 ms | 81 ms |
| Split | <1 ms | <1 ms |

The cost is flat because:

- **Import** calls `ffprobe` on headers only. Nothing is decoded.
- **Preview** uses **libmpv** with `hwdec=d3d11va` and `vo=gpu-next`: GPU
  decoding, streamed from disk on demand. The entire timeline is handed to mpv as
  a single **EDL**, so it plays as one continuous video and clip boundaries are
  invisible.
- **Keyframe search** uses `ffprobe -read_intervals` over a few-second window
  around the cut instead of walking the file.
- **Thumbnails** are generated only for the visible range, on three threads, with
  direct seeks and an on-disk cache.

## The two export modes, and why there is no third

You pick one. **They cannot be mixed**, and that is the most interesting finding
in the project.

### Stream copy (default)

Cuts are snapped to the nearest keyframe and everything is copied bit-for-bit.
The video is byte-identical to the source and export runs at disk speed — a 20 GB
file takes a couple of minutes.

The cost: each cut can move by up to half a GOP. With a typical camera (keyframe
every 5–10 s) that is **several seconds**. The export dialog tells you exactly
how far every single cut will move before you confirm.

### Frame-exact

Everything is re-encoded with NVENC. The cut lands exactly where you put it.
Measured (RTX 5080 Laptop, 1280×720 @ 29.97 fps):

| Encoder | Speed | 1 h of footage | Size |
|---|---|---|---|
| NVENC H.264 | 9.9× realtime | ~6 min | ×1.35 |
| NVENC HEVC | 7.8× realtime | ~8 min | ×1.35 |
| libx264 (CPU) | 9.5× realtime | ~6 min | ×1.21 |

Quality defaults to matching the source bitrate. At a fixed QP 18 quality is
higher but the file is **twice the size** — rarely worth it on 20 GB recordings.

### Why you can't have both

The first version did the obvious thing: re-encode only the fragment up to the
keyframe, copy the rest. **It produces corrupt video**, and here is the measured
reason:

- An H.264 stream carries its encoding parameters in SPS/PPS headers.
- MP4, MKV and MOV store **one set of those per track**.
- The NVENC fragment and the camera's copied segment never have the same ones.
  Concatenating them by copy means everything after the first piece is decoded
  with parameters that belong to something else.
- Exact symptom: `reference count overflow`, the image freezes after every cut
  until the next keyframe, and the audio keeps playing normally.

MPEG-TS, MKV, MP4 and the `h264_mp4toannexb` filter were all tested: all six
paths yield between **913 and 2761 decode errors** (`tests/diag_mix.py`). Pieces
in MPEG-TS additionally arrive with no SPS/PPS headers at all
(`tests/diag_nal.py`). This is not a misconfiguration — it is a format limit.

If clips disagree on codec, resolution, fps or rotation they cannot be copied;
the app falls back to frame-exact mode automatically and says why.

### Other details that matter

- Intermediate pieces use **Matroska**, which stores extradata per track and
  leaves each piece independently decodable.
- Each piece's end is bounded by **packet count, not time**: with B-frames,
  decode order isn't presentation order, and cutting by time drags an extra
  frame or two into every cut.
- **90° rotation** is written as container metadata — no re-encoding.
- **Audio** is assembled in a single pass for the whole timeline. Doing it
  per-piece would add the AAC encoder's priming delay at every cut; across
  fifteen cuts that is nearly half a second of drift.
- After export, a few seconds around **each junction are decoded** to confirm the
  image isn't broken. It's cheap (it never reads the whole file) and it exists
  precisely because this bug once slipped through: the file had the correct
  duration and the correct frame count and was still broken.

## Security

The app is local-only — no network listener, no server, no accounts. But it
executes bundled third-party binaries with the user's privileges, and it parses
two kinds of untrusted input: `.swproj` project files and filenames.

It was audited against a written threat model and an 18-point checklist derived
from OWASP ASVS. **11 findings, all fixed, all regression-tested.** Highlights:

- **Argument and protocol injection into ffmpeg.** The code was already immune to
  *shell* injection; the real hole was one layer down. `ffprobe` takes its input
  as the last positional argument, so a file named `-loglevel` parsed as an
  option — and ffmpeg resolves input names as URLs, so `concat:` and `http://`
  were valid "filenames" reachable from a shared project file. Inputs are now
  validated and pinned to `file:`.
- **`.swproj` parsed without validation** — including `json.loads` silently
  accepting `NaN`, which propagates through every duration calculation without
  ever raising.
- **Injection into ffmpeg's concat list** from a destination folder containing an
  apostrophe. The fix is verified by round-tripping through `shlex`.
- **Process-wide `PATH` widening.** The app prepended its own `bin/` to `PATH`
  permanently to let `python-mpv` find its DLL — so every ffmpeg spawned
  afterwards inherited an app-controlled directory first on its `PATH`. Now
  scoped to the `import` statement and reverted in a `finally`.
- **Integrity of `bin/`.** 307 MB of ffmpeg/libmpv is now pinned by
  `bin/SHA256SUMS` and verified at startup — *before* the import that loads
  `libmpv-2.dll`, since verifying afterwards would mean verifying code that had
  already run. Cost: 0.16 s.

Full threat model, checklist and per-finding write-up: **[SECURITY.md](SECURITY.md)**.

## Tests

```
run_tests.bat
```

144 checks:

- `tests/test_security.py` — **48**: input validation, path handling, concat
  quoting, project-file hardening, binary integrity. One block per finding in
  SECURITY.md, so removing a defence later shows up here.
- `tests/test_pipeline.py` — **56**: metadata, keyframes, editing, planning for
  both modes, and real exports verified frame by frame.
- `tests/test_freeze.py` — **8**: *the key test*. Uses footage with irregular
  keyframes and open GOPs, exports in both modes, decodes the entire result and
  verifies there are no errors and no frozen segments. It detects repeated frames
  by hash, so it does not depend on anyone watching.
- `tests/test_gui.py` — **32**: opens the real window, loads mpv, and chains
  import, split, delete, undo, rotate, reorder and save.

Benchmarks and diagnostics (not part of `run_tests.bat`):

- `tests/bench_large.py` — cost does not grow with file size. Needs
  `python tests\make_samples.py --big` (generates ~1.3 GB).
- `tests/bench_encode.py` — frame-exact mode speed and size on this GPU.
- `tests/diag_mix.py`, `tests/diag_nal.py` — the evidence for why copy and
  re-encode cannot be mixed.
- `tests/diag_where.py` — isolates which pipeline stage introduces a corruption.
- `tests/diag_player.py` — dumps mpv's internal state.

## Layout

```
app/
  main.py             entry point; verifies bin/ before anything loads
  main_window.py      window, toolbar, shortcuts, export flow
  player.py           embedded libmpv + EDL generation
  timeline_widget.py  hand-drawn timeline (drag, zoom, filmstrip)
  thumbs.py           lazy cached thumbnails
  exporter.py         export planner and executor
  model.py            sources, clips, timeline, undo, project I/O
  ffmpeg_tools.py     ffmpeg/ffprobe wrapper, path validation, integrity check
bin/                  ffmpeg.exe, ffprobe.exe, libmpv-2.dll (not distributed)
cache/                thumbnails and preview EDL (disposable)
```

`timeline_widget.py` paints with `QPainter` rather than using `QGraphicsView`:
the content is a single row of rectangles, and direct repaint control avoids
useless work when the playhead moves 60 times a second.

## Shortcuts

| Key | Action |
|---|---|
| `Space` | Play / pause |
| `S` | Split at playhead |
| `Del` | Delete selected clip |
| `R` | Rotate clip 90° |
| `←` `→` | Frame by frame |
| `Shift` + `←` `→` | Jump 2 seconds |
| `Home` / `End` | Go to start / end |
| `Ctrl` + wheel | Zoom the timeline |
| `F` | Fit zoom to project |
| `Ctrl+Z` / `Ctrl+Y` | Undo / redo |
| `Ctrl+O` / `Ctrl+S` | Import video / save project |

## Setup

Python 3.14, plus `PySide6` and `python-mpv`:

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The binaries in `bin/` are **not distributed with this repository** — they are
307 MB of third-party code. Download them yourself and verify against the pinned
hashes in `bin/SHA256SUMS` (the app checks on every launch and refuses to start
on a mismatch):

- `ffmpeg.exe` / `ffprobe.exe` — gyan.dev Windows build, with NVENC (h264/hevc/av1)
- `libmpv-2.dll` — `shinchiro/mpv-winbuild-cmake`

See [SECURITY.md §4](SECURITY.md) for the verification command and what to do
when updating them.

## Project format

`.swproj` is JSON holding video paths and cut points. It **does not copy the
footage**: if you move or delete the source videos, opening the project tells you
which ones are missing. Because it is shareable, it is parsed as untrusted input
— see F2 in SECURITY.md.

## License

MIT.
