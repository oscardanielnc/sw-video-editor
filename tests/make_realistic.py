"""Genera material parecido al de una grabacion real.

Los primeros videos de prueba tenian keyframes en tiempos redondos (9.0, 12.0)
porque se forzo -g fijo a 30 fps exactos. Una grabacion de camara o de OBS no es
asi: 29.97 fps, GOP largo, keyframes irregulares por deteccion de escena y GOP
abierto. Justo ahi es donde aparecen los fallos de corte.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ffmpeg_tools import FFMPEG, run  # noqa: E402

OUT = Path(__file__).resolve().parent / "samples"


def make_realistic(name: str = "realistic_90s.mp4", seconds: int = 90) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    if path.exists():
        return path

    # 29.97 fps -> los keyframes caen en tiempos como 10.2435767, no en 10.0.
    # GOP de 10 s con corte por escena y GOP abierto, como una camara real.
    args = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i",
        f"testsrc2=size=1280x720:rate=30000/1001:duration={seconds}",
        "-f", "lavfi", "-i",
        f"sine=frequency=440:sample_rate=48000:duration={seconds}",
        "-vf", "drawtext=text='%{pts\\:hms}':fontsize=72:fontcolor=white"
               ":x=(w-tw)/2:y=(h-th)/2:box=1:boxcolor=black@0.5",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-g", "300", "-keyint_min", "25",
        "-x264opts", "open-gop=1:bframes=3:b-pyramid=normal",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
        "-movflags", "+faststart", str(path),
    ]
    res = run(args, timeout=900)
    if res.returncode != 0:
        raise SystemExit(f"Fallo generando {name}:\n{res.stderr[-1500:]}")
    return path


if __name__ == "__main__":
    p = make_realistic()
    print(p, p.stat().st_size, "bytes")
