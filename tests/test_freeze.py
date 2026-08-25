"""Detecta imagen congelada y errores de decodificacion en el video exportado.

Reproduce el fallo que se ve al reproducir el resultado: tras cada corte la
imagen se queda pegada varios segundos mientras el audio sigue. La deteccion es
objetiva: se calcula el hash de cada fotograma decodificado y se buscan rachas de
hashes identicos. El material de prueba lleva un cronometro impreso, asi que dos
fotogramas iguales seguidos solo pueden significar imagen congelada.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from app.exporter import (MODE_EXACT, MODE_LOSSLESS, ExportWorker,  # noqa: E402
                          build_plan)
from app.ffmpeg_tools import FFMPEG, probe, run  # noqa: E402
from app.model import Timeline  # noqa: E402

SAMPLES = Path(__file__).resolve().parent / "samples"
OUT = Path(__file__).resolve().parent / "out"
REAL = str(SAMPLES / "realistic_90s.mp4")

_fails: list[str] = []
_passes = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _passes
    if ok:
        _passes += 1
        print(f"  OK   {name}")
    else:
        _fails.append(f"{name}: {detail}")
        print(f"  FAIL {name}  -> {detail}")


def decode_errors(path: str) -> list[str]:
    """Decodifica el archivo entero y devuelve los errores que reporte ffmpeg."""
    res = run([FFMPEG, "-hide_banner", "-v", "error", "-nostdin",
               "-i", path, "-f", "null", "-"], timeout=1800)
    return [ln for ln in (res.stderr or "").splitlines() if ln.strip()]


def frozen_runs(path: str, fps: float) -> list[tuple[float, int]]:
    """Devuelve [(segundo, fotogramas repetidos)] de cada tramo congelado."""
    res = run([FFMPEG, "-hide_banner", "-v", "quiet", "-nostdin", "-i", path,
               "-map", "0:v:0", "-f", "framehash", "-hash", "md5", "-"],
              timeout=1800)
    hashes: list[str] = []
    for line in (res.stdout or "").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split(",")
        if len(parts) >= 6:
            hashes.append(parts[-1].strip())

    runs: list[tuple[float, int]] = []
    i = 0
    while i < len(hashes):
        j = i
        while j + 1 < len(hashes) and hashes[j + 1] == hashes[i]:
            j += 1
        length = j - i + 1
        # Tres fotogramas identicos ya no es casualidad en este material.
        if length >= 3:
            runs.append((i / fps, length))
        i = j + 1
    return runs


def export_sync(tl: Timeline, out: str, **kw) -> tuple[bool, str]:
    worker = ExportWorker(tl, out, **kw)
    result: dict = {}
    worker.finished_ok.connect(lambda p, s: result.update(ok=True, msg=s))
    worker.failed.connect(lambda m: result.update(ok=False, msg=m))
    try:
        worker._export()
    except Exception as exc:
        result.update(ok=False, msg=str(exc))
    finally:
        worker._cleanup()
    return bool(result.get("ok")), str(result.get("msg", "sin resultado"))


def build_timeline() -> Timeline:
    """Tres cortes, todos deliberadamente lejos de un keyframe."""
    tl = Timeline()
    tl.append_source(REAL)
    tl.split_at(15.3)
    tl.split_at(45.7)
    tl.delete(tl.clips[1].cid)      # borra el tramo del medio
    tl.move(tl.clips[-1].cid, 0)    # y reordena
    return tl


def run_mode(mode: str, label: str, src) -> None:
    print(f"\n--- Modo {label} ---")
    tl = build_timeline()
    plan = build_plan(tl, mode)
    print(f"Plan: {len(plan.pieces)} trozos, modo resultante '{plan.mode}', "
          f"{plan.total_duration:.2f} s")
    for i, pz in enumerate(plan.pieces):
        print(f"   {i} {pz.mode:6s} [{pz.start:8.4f} -> {pz.end:8.4f}]")
    if plan.shift_detail():
        print("   desplazamiento de cortes: " + "; ".join(plan.shift_detail()))

    out = str(OUT / f"freeze_{mode}.mp4")
    ok, msg = export_sync(tl, out, mode=mode)
    check(f"[{label}] la exportacion termina bien", ok, msg)
    if not ok:
        return

    got = probe(out)
    check(f"[{label}] duracion coherente con el plan (+-0.3 s)",
          abs(got.duration - plan.total_duration) < 0.3,
          f"esperado {plan.total_duration:.2f}, obtenido {got.duration:.2f}")

    errs = decode_errors(out)
    # Un puñado de avisos al arrancar un GOP abierto es normal; decenas no.
    check(f"[{label}] el video decodifica sin errores", len(errs) <= 3,
          f"{len(errs)} errores, p.ej.: {errs[0][:110] if errs else ''}")

    runs = frozen_runs(out, src.fps)
    worst = max((n for _, n in runs), default=0)
    if runs:
        print("       tramos congelados detectados:")
        for t, n in runs[:8]:
            print(f"         segundo {t:6.2f}  ->  {n} fotogramas iguales "
                  f"({n / src.fps:.2f} s)")
    check(f"[{label}] no hay imagen congelada", not runs,
          f"{len(runs)} tramos, el peor de {worst} fotogramas "
          f"({worst / src.fps:.2f} s)")


def main() -> int:
    QCoreApplication(sys.argv)
    if not Path(REAL).exists():
        print("Falta realistic_90s.mp4: ejecuta antes tests/make_realistic.py")
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    src = probe(REAL)
    print(f"Fuente: {src.width}x{src.height} @ {src.fps:.3f} fps, "
          f"{src.duration:.2f} s, keyframes cada ~10 s (GOP abierto)")

    run_mode(MODE_LOSSLESS, "sin recodificar", src)
    run_mode(MODE_EXACT, "exacto al fotograma", src)

    print("\n" + "=" * 64)
    print(f"{_passes} comprobaciones correctas, {len(_fails)} fallos")
    for f in _fails:
        print("  FAIL " + f)
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
