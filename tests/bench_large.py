"""Comprueba que el coste de abrir y editar NO depende del tamanio del archivo.

Es la garantia de que un video de 20 GB no deja la aplicacion colgada: nada de
lo que se hace al importar o al cortar lee el archivo entero.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ffmpeg_tools import human_size, keyframe_bracket, probe  # noqa: E402
from app.model import Timeline  # noqa: E402

SAMPLES = Path(__file__).resolve().parent / "samples"
BIG = SAMPLES / "big_1h.mp4"
SMALL = SAMPLES / "landscape_60s.mp4"

LIMITS = {"importar": 3.0, "buscar keyframes": 4.0, "dividir": 0.2}
_fails: list[str] = []


def timed(label: str, limit: float, fn):
    t0 = time.perf_counter()
    result = fn()
    dt = time.perf_counter() - t0
    ok = dt <= limit
    print(f"  {'OK  ' if ok else 'LENTO'} {label:34s} {dt * 1000:8.1f} ms "
          f"(limite {limit * 1000:.0f} ms)")
    if not ok:
        _fails.append(f"{label}: {dt:.2f} s > {limit} s")
    return result


def main() -> int:
    if not BIG.exists():
        print("Falta tests/samples/big_1h.mp4")
        return 2

    for path in (SMALL, BIG):
        src_size = path.stat().st_size
        print(f"\n{path.name}  ({human_size(src_size)})")

        src = timed("importar (leer metadatos)", LIMITS["importar"],
                    lambda: probe(str(path)))
        print(f"       duracion {src.duration / 60:.1f} min, "
              f"{src.width}x{src.height} @ {src.fps:.0f} fps")

        # Un punto muy adentro del archivo: es el caso peor para el seek.
        deep = src.duration * 0.85
        kf = timed(f"keyframes en el minuto {deep / 60:.0f}",
                   LIMITS["buscar keyframes"],
                   lambda: keyframe_bracket(str(path), deep, src.fps))
        print(f"       keyframes {kf[0]:.2f} / {kf[1] if kf[1] else '-'}")

        tl = Timeline()
        tl.append_source(str(path))
        timed("dividir en la linea de tiempo", LIMITS["dividir"],
              lambda: tl.split_at(deep))
        print(f"       clips ahora: {len(tl.clips)}")

    print("\n" + "=" * 64)
    if _fails:
        print(f"{len(_fails)} operaciones por encima del limite:")
        for f in _fails:
            print("  " + f)
        return 1
    print("El coste no escala con el tamanio del archivo: correcto.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
