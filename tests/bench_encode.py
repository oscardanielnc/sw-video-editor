"""Mide la velocidad real del modo 'exacto al fotograma' en esta GPU.

Sirve para saber que esperar al recodificar una grabacion de varias horas.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from app.exporter import MODE_EXACT, ExportWorker  # noqa: E402
from app.ffmpeg_tools import human_size, probe  # noqa: E402
from app.model import Timeline  # noqa: E402

SAMPLES = Path(__file__).resolve().parent / "samples"
OUT = Path(__file__).resolve().parent / "out"
REAL = str(SAMPLES / "realistic_90s.mp4")


def main() -> int:
    QCoreApplication(sys.argv)
    if not Path(REAL).exists():
        print("Falta realistic_90s.mp4: ejecuta tests/make_realistic.py")
        return 2
    OUT.mkdir(parents=True, exist_ok=True)

    src = probe(REAL)
    tl = Timeline()
    tl.append_source(REAL)

    combos = [("h264_nvenc", 0, "NVENC H.264 · auto"),
              ("h264_nvenc", 18, "NVENC H.264 · QP18"),
              ("hevc_nvenc", 0, "NVENC HEVC · auto"),
              ("libx264", 0, "libx264 · auto")]

    for encoder, quality, label in combos:
        out = OUT / f"bench_{encoder}_{quality}.mp4"
        worker = ExportWorker(tl, str(out), mode=MODE_EXACT, quality=quality,
                              encoder=encoder)
        t0 = time.perf_counter()
        try:
            worker._export()
        except Exception as exc:
            print(f"  {label:22s} -> fallo: {exc}")
            continue
        finally:
            worker._cleanup()
        dt = time.perf_counter() - t0

        got = probe(str(out))
        speed = src.duration / dt
        size_ratio = out.stat().st_size / Path(REAL).stat().st_size
        print(f"  {label:22s} {dt:6.1f} s  ->  {speed:5.1f}x tiempo real  "
              f"({got.width}x{got.height})  tamanio x{size_ratio:.2f} "
              f"({human_size(out.stat().st_size)})")
        # Extrapolacion a una grabacion larga como las del uso real.
        for hours in (1, 2):
            print(f"        una grabacion de {hours} h tardaria "
                  f"~{hours * 3600 / speed / 60:.0f} min")

    return 0


if __name__ == "__main__":
    print(f"Recodificando {probe(REAL).duration:.0f} s de 1280x720 @ 29.97 fps\n")
    sys.exit(main())
