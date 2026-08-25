"""Localiza en que punto entra la corrupcion: al generar cada trozo o al unirlos."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from app.exporter import ExportWorker, build_plan  # noqa: E402
from app.ffmpeg_tools import FFMPEG, FFPROBE, probe, run  # noqa: E402
from app.model import Timeline  # noqa: E402

SAMPLES = Path(__file__).resolve().parent / "samples"
OUT = Path(__file__).resolve().parent / "out"
REAL = str(SAMPLES / "realistic_90s.mp4")


def dec_errors(path: str) -> tuple[int, str]:
    res = run([FFMPEG, "-hide_banner", "-v", "error", "-nostdin",
               "-i", path, "-f", "null", "-"], timeout=900)
    lines = [x for x in (res.stderr or "").splitlines() if x.strip()]
    return len(lines), (lines[0][:100] if lines else "")


def head_frames(path: str, n: int = 5) -> list:
    res = run([FFPROBE, "-v", "error", "-select_streams", "v:0",
               "-show_entries", "frame=pts_time,key_frame,pict_type",
               "-print_format", "json", "-of", "json", path], timeout=300)
    try:
        frames = json.loads(res.stdout or "{}").get("frames", [])
    except json.JSONDecodeError:
        return []
    return [(f.get("pts_time"), f.get("key_frame"), f.get("pict_type"))
            for f in frames[:n]]


def main() -> None:
    QCoreApplication(sys.argv)
    OUT.mkdir(parents=True, exist_ok=True)

    n, first = dec_errors(REAL)
    print(f"FUENTE original: {n} errores al decodificar  {first}\n")

    tl = Timeline()
    tl.append_source(REAL)
    tl.split_at(15.3)
    tl.split_at(45.7)
    tl.delete(tl.clips[1].cid)
    tl.move(tl.clips[-1].cid, 0)

    plan = build_plan(tl)
    worker = ExportWorker(tl, str(OUT / "where.mp4"))
    worker.tmp_dir = OUT / "wheretmp"
    worker.tmp_dir.mkdir(parents=True, exist_ok=True)

    parts = []
    done = 0.0
    print("TROZOS por separado:")
    for i, piece in enumerate(plan.pieces):
        part = worker.tmp_dir / f"p{i:04d}.mkv"
        worker._run_piece(piece, plan, part, done, plan.total_duration, f"p{i}")
        done += piece.out_duration
        n, first = dec_errors(str(part))
        print(f"  {i} {piece.mode:6s} [{piece.start:8.4f} -> {piece.end:8.4f}]  "
              f"errores={n:5d}  {first}")
        print(f"      primeros fotogramas: {head_frames(str(part))}")
        parts.append(part)

    audio = worker._build_audio(plan)
    worker._concat(parts, plan, audio)
    n, first = dec_errors(str(OUT / "where.mp4"))
    print(f"\nFINAL tras unir: {n} errores  {first}")


if __name__ == "__main__":
    main()
