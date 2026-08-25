"""Genera videos de prueba con keyframes espaciados, como una grabacion real."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ffmpeg_tools import FFMPEG, run  # noqa: E402

OUT = Path(__file__).resolve().parent / "samples"


def make(name: str, seconds: int, w: int, h: int, fps: int, gop: int,
         color: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    if path.exists():
        return path
    args = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i",
        f"testsrc2=size={w}x{h}:rate={fps}:duration={seconds}",
        "-f", "lavfi", "-i",
        f"sine=frequency=440:sample_rate=48000:duration={seconds}",
        "-vf", (f"drawtext=text='%{{pts\\:hms}}':fontsize={h // 10}:fontcolor={color}"
                f":x=(w-tw)/2:y=(h-th)/2:box=1:boxcolor=black@0.5"),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
        "-movflags", "+faststart", str(path),
    ]
    res = run(args, timeout=600)
    if res.returncode != 0:
        raise SystemExit(f"Fallo generando {name}:\n{res.stderr[-1500:]}")
    return path


def make_big(source: Path, repeats: int = 60) -> Path:
    """Archivo largo y pesado para bench_large.py.

    Se construye repitiendo el corto por copia de flujo: da 1 hora y mas de 1 GB
    en segundos, sin recodificar nada.
    """
    path = OUT / "big_1h.mp4"
    if path.exists():
        return path
    listing = OUT / "big_list.txt"
    listing.write_text("\n".join(f"file '{source.name}'" for _ in range(repeats)),
                       encoding="ascii", newline="\n")
    res = run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "concat",
               "-safe", "0", "-i", str(listing), "-c", "copy", "-y", str(path)],
              timeout=900)
    if res.returncode != 0:
        raise SystemExit(f"Fallo generando big_1h.mp4:\n{res.stderr[-1500:]}")
    return path


if __name__ == "__main__":
    a = make("landscape_60s.mp4", 60, 1280, 720, 30, 90, "white")   # keyframe cada 3 s
    b = make("landscape_30s.mp4", 30, 1280, 720, 30, 60, "yellow")  # keyframe cada 2 s
    c = make("portrait_20s.mp4", 20, 720, 1280, 30, 90, "cyan")
    for p in (a, b, c):
        print(p, p.stat().st_size, "bytes")

    if "--big" in sys.argv:
        big = make_big(a)
        print(big, f"{big.stat().st_size / 1024**3:.2f} GB")
    else:
        print("\n(anade --big para generar tambien big_1h.mp4, ~1.3 GB, "
              "necesario para tests/bench_large.py)")
