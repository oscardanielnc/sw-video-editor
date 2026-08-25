# Security

This document records the threat model for SW Video Editor, the checklist the
code was audited against, every finding, and how each one was fixed and is now
regression-tested.

The honest summary first: **this is a local desktop application with no network
listener, no server, no authentication and no user accounts.** Nothing here is
remotely exploitable. Ratings below are relative to the one attack path that
actually exists — someone hands you a file — and are deliberately not inflated.

---

## 1. Threat model

### What the app does

A Qt/PySide6 desktop editor for very large video files (10–20 GB). It shells out
to bundled `ffmpeg`/`ffprobe` binaries and embeds `libmpv` for preview. It runs
with the privileges of the user who launched it.

### Trust boundaries

| Boundary | Input | Trusted? |
|---|---|---|
| File dialog / drag-and-drop | Video file paths | Path: yes (user picked it). **Contents and filename: no.** |
| Command line (`run.bat video.mp4`) | Paths | **No** — a `.bat` can be invoked by anything |
| `.swproj` project file | JSON: paths, cut points, rotation | **No** — a project file is shareable, so it is attacker-controlled data |
| `bin/` | ffmpeg, ffprobe, libmpv | **No** — 307 MB of third-party code, downloaded out-of-band |
| Export destination | Directory chosen by the user | Path is trusted; **the string is not** |

### Attacker model

Someone who can hand the user a file — a `.swproj`, a video with a crafted name,
or a tampered `bin/` — and get them to open it. That is the entire attack
surface, and it is the surface the audit covers.

### Explicitly out of scope

- **Malicious video *content*.** A crafted H.264 stream that exploits a decoder
  bug is an ffmpeg/libmpv vulnerability, not one this app can fix. The mitigation
  is keeping `bin/` current — which is exactly why §4 exists.
- Local privilege escalation. The app holds no secrets and grants no privileges
  it did not already have.
- Anything network-facing. There is none.

---

## 2. Checklist used

Derived from OWASP ASVS v4 (the sections that apply to a local, non-web
application) plus desktop- and Windows-specific items ASVS does not cover.

| # | Check | Result |
|---|---|---|
| C1 | No shell interpolation when spawning processes | ✅ Already correct — list-form `subprocess`, never `shell=True` |
| C2 | Child executables resolved by absolute path, not `PATH` | ❌ **F4** |
| C3 | Untrusted strings cannot become command-line *options* | ❌ **F1** |
| C4 | Untrusted strings cannot select a different *protocol/handler* | ❌ **F1** |
| C5 | Untrusted structured input is schema-validated before use | ❌ **F2** |
| C6 | Numeric input is range- and finiteness-checked | ❌ **F2** |
| C7 | Resource limits on untrusted input (size, item count) | ❌ **F2** |
| C8 | Generated config/list files escape their own syntax | ❌ **F3** |
| C9 | Untrusted text is not rendered as markup | ❌ **F5** |
| C10 | Third-party binaries have pinned, verifiable integrity | ❌ **F6** |
| C11 | Library search paths are not widened process-wide | ❌ **F7** |
| C12 | Temp files/dirs created unpredictably and exclusively | ❌ **F8** |
| C13 | Embedded engines run with scripting/network disabled | ❌ **F9** |
| C14 | Cache keys cannot collide across distinct inputs | ⚠️ **F10** |
| C15 | Child processes cannot block on stdin | ⚠️ **F11** |
| C16 | No `eval`/`exec`/`pickle` on untrusted data | ✅ Already correct — `json` only, no `pickle` anywhere |
| C17 | Secrets in source or logs | ✅ N/A — the app has none |
| C18 | Errors do not leak paths to a third party | ✅ N/A — errors are shown locally only |

Result: **14 failed checks, 11 distinct findings** (F1 and F2 each
cover several checks). All fixed. No finding was accepted as-is.

---

## 3. Findings

Severity is **contextual to this app**, not CVSS. "High" here means *the most
serious thing that can happen to a local editor*, not remote code execution.

### F1 — Argument and protocol injection into ffmpeg — High

`ffprobe` takes the input filename as its **last positional argument**, so a file
named `-loglevel` was parsed as an *option*, not a file. Worse, ffmpeg resolves
input names as URLs: `http://`, `concat:`, `subfile:` and `crypto:` are all valid
input "filenames". A `.swproj` carries paths, so that string is attacker-supplied
— a crafted project could have made the app fetch a remote URL or read a file
through a protocol handler.

Note the pre-existing code was already immune to *shell* injection (list-form
`subprocess`, never `shell=True`). This is the subtler layer underneath: argument
injection into the program itself.

**Fix** — `app/ffmpeg_tools.py`:

- `safe_path()` — rejects anything that does not `resolve(strict=True)` to an
  existing regular file. Protocol strings and URLs never survive this.
- `as_input()` — emits `file:C:/…`, pinning the protocol explicitly so a leading
  `-` can never be read as an option.

Every `-i` and every positional input in `ffmpeg_tools.py` and `exporter.py` now
goes through these. 18 regression checks.

### F2 — `.swproj` parsed without validation — High

`load()` did `json.loads`, then indexed `item["path"]` and `float(item["in"])`
directly. Consequences:

- Missing keys → unhandled `KeyError` crash.
- `json.loads` **accepts `NaN` and `Infinity`** (non-standard, on by default).
  A `NaN` duration propagates silently through every timeline calculation — no
  exception, just permanently wrong output.
- No cap on clip count or file size → trivial resource exhaustion.
- `rotate` was cast with `int()`, accepting any integer.

**Fix** — `app/model.py`: size check *before* parsing, `version` pinned, every
field type- and range-checked, `_finite()` rejects NaN/±Infinity, `MAX_CLIPS`
and `MAX_PROJECT_BYTES` caps, `rotate` restricted to `{0, 90, 180, 270}`, and
saved cut points clamped to the file's real duration (the video may have changed
since the project was saved). A genuinely missing video is still just a warning,
not an error. 20 regression checks.

### F3 — Injection into the ffmpeg concat list — Medium

Export builds a listing file for ffmpeg's concat demuxer:

```python
"\n".join(f"file '{p.as_posix()}'" for p in parts)   # before
```

The demuxer reads that file **line by line as directives**. The paths derive from
the user-chosen output directory, so a folder named `Bob's videos` closed the
quoted literal early and the rest of the path was parsed as demuxer directives —
alongside `-safe 0`, which the concat step needs.

**Fix** — `_concat_quote()` in `app/exporter.py` applies ffmpeg's documented
escaping (`'` → `'\''`). Tested by **round-tripping through `shlex`**: the quoted
form must parse back to exactly one token equal to the original.

### F4 — Executable resolved via `PATH` — Medium (Windows)

```python
subprocess.Popen(["explorer", "/select,", str(Path(path))])   # before
```

Windows `CreateProcess` searches the application directory and the current
working directory **before** `PATH`. A file named `explorer.exe` dropped next to
the app would have won that resolution.

**Fix** — `MainWindow._reveal()` resolves `%SystemRoot%\explorer.exe` by absolute
path, verifies it is a file, and passes the target as a separate argument.

### F5 — Untrusted text rendered as markup in Qt dialogs — Low

`QLabel` renders rich text, and `QMessageBox` defaults to `Qt::AutoText`, which
*auto-detects* markup. The export dialog interpolated clip names, ffmpeg warning
text and exception strings straight into an HTML string, and the message boxes
displayed paths and exception strings with the format left to Qt's guess.

**Reachability, stated honestly:** the obvious vector — a file literally named
`<img src="file:///…">.mp4` — **does not work on Windows**, because the OS
rejects `<` and `>` in filenames outright (verified, not assumed). What remains
reachable is the text that is *not* a filename: ffmpeg/ffprobe warning and error
strings, which are derived from the metadata inside a video file and therefore
attacker-influenced. That is a narrow vector on a local app, hence Low. It is
also the kind of thing that stops being Low the day the code is ported to a
platform with laxer filename rules.

**Fix** — `html.escape()` on every interpolated value in `ExportDialog._analyze`,
with markup added only by the app itself; and a `_message()` helper that forces
`setTextFormat(Qt.PlainText)` on every message box that displays a path, a
filename or an exception string. Verified by driving the real dialog with markup
in the plan warnings and asserting it comes out escaped.

### F6 — Unverifiable third-party binaries — Medium (supply chain)

`bin/` holds 307 MB of ffmpeg, ffprobe and libmpv that the app **executes with
the user's privileges**, with no hashes, no versions and a README that said to
"re-download them by hand". Anyone cloning the repo had no way to distinguish a
correct download from a substituted one. This is the highest-impact finding for a
project that is about to be published.

**Fix**:

- `bin/SHA256SUMS` is versioned; the binaries are **not** distributed (see
  `.gitignore`). The manifest is the trust anchor.
- `verify_binaries()` hashes all three against the manifest **at startup, before
  the import that loads `libmpv-2.dll`** — verifying after loading would mean
  verifying code that had already run.
- A mismatch blocks launch with an explanation. Cost: **0.16 s**.

### F7 — Process-wide `PATH` widening — Medium

`player.py` prepended `bin/` to `os.environ["PATH"]` permanently, because
`python-mpv` locates its DLL via `ctypes.util.find_library`, which scans `PATH`.
The environment is inherited, so **every ffmpeg the app spawned afterwards ran
with an app-controlled directory first on its `PATH`**.

**Fix** — the widening is now scoped to the `import mpv` statement in a
`try/finally` and reverted immediately. The DLL stays loaded in the process; the
children do not inherit the widened `PATH`. As a bonus this also guarantees *our*
`libmpv-2.dll` wins over any other copy already on the system `PATH`.

### F8 — Predictable temp directory — Low

`.swtmp_{int(time.time())}` next to the output, created with `exist_ok=True` —
a predictable name that would happily reuse a directory someone else created
first. Relevant because the export destination may be a shared drive.

**Fix** — `tempfile.mkdtemp(prefix=".swtmp_", dir=…)`, which creates with
`O_EXCL` and a random suffix. It stays next to the destination, preserving the
original design goal of never copying tens of GB across drives.

### F9 — libmpv not explicitly hardened — Low

The player relied on libmpv's defaults rather than stating its own. Defaults are
a moving target across versions, and this player only ever needs to open local
files.

**Fix** — `config=False`, `load_scripts=False`, `ytdl=False`, `osd_level=0`:
no user config, no Lua/JS scripts, no network fetching.

### F10 — Truncated SHA-1 cache keys — Low

Thumbnail cache filenames were `sha1(...)[:20]`. Not a security boundary, but a
collision serves the wrong video's frame, and truncated SHA-1 is the wrong
default to leave in a published codebase.

**Fix** — `sha256(...)[:32]`.

### F11 — Children inherited stdin — Low

`run()` and `popen()` left stdin attached. `ffmpeg` prompts on stdin (e.g. for
file overwrites); a prompt in a GUI child process is an unkillable hang.

**Fix** — `stdin=subprocess.DEVNULL` on both helpers.

---

## 4. Keeping it secure

Verify `bin/` against the manifest at any time:

```powershell
cd bin
Get-FileHash -Algorithm SHA256 ffmpeg.exe, ffprobe.exe, libmpv-2.dll |
    ForEach-Object { "$($_.Hash.ToLower())  $(Split-Path $_.Path -Leaf)" }
Get-Content SHA256SUMS
```

The app does this for you on every launch. The regression suite also asserts it:

```
run_tests.bat        # 48 security checks + 96 functional checks
```

**When you update ffmpeg or libmpv**, regenerate the manifest *from the download
you verified against the publisher*, and record the new version in the README.
The manifest is only as trustworthy as the download it was generated from.

Sources currently pinned:

- `ffmpeg.exe` / `ffprobe.exe` — gyan.dev Windows build, with NVENC (h264/hevc/av1)
- `libmpv-2.dll` — `shinchiro/mpv-winbuild-cmake`

## 5. Reporting a vulnerability

Open an issue. Given the threat model there is nothing here worth embargoing, so
public disclosure is fine and preferred.
