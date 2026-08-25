"""Diagnostico: cuenta fotogramas de cada trozo intermedio y del archivo final."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from app.exporter import ExportWorker, build_plan  # noqa: E402
from app.ffmpeg_tools import FFPROBE, run  # noqa: E402
from app.model import Timeline  # noqa: E402

SAMPLES = Path(__file__).resolve().parent / "samples"
OUT = Path(__file__).resolve().parent / "out"
LAND60 = str(SAMPLES / "landscape_60s.mp4")


def frames(path: str) -> int:
    res = run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-count_frames",
               "-show_entries", "stream=nb_read_frames", "-print_format", "json",
               path], timeout=600)
    try:
        return int(json.loads(res.stdout)["streams"][0]["nb_read_frames"])
    except Exception:
        return -1


def stream_durations(path: str) -> dict:
    res = run([FFPROBE, "-v", "error", "-show_entries",
               "stream=codec_type,duration,nb_frames:format=duration",
               "-print_format", "json", path], timeout=120)
    data = json.loads(res.stdout)
    out = {"format": data["format"].get("duration")}
    for s in data["streams"]:
        out[s["codec_type"]] = (s.get("duration"), s.get("nb_frames"))
    return out


def main() -> None:
    QCoreApplication(sys.argv)
    OUT.mkdir(parents=True, exist_ok=True)

    tl = Timeline()
    tl.append_source(LAND60)
    tl.split_at(10.4)
    tl.split_at(35.7)
    tl.delete(tl.clips[1].cid)
    tl.move(tl.clips[-1].cid, 0)

    plan = build_plan(tl)
    print(f"Duracion esperada: {plan.total_duration:.4f} s "
          f"({round(plan.total_duration * 30)} fotogramas a 30 fps)\n")

    worker = ExportWorker(tl, str(OUT / "diag.mp4"))
    worker.tmp_dir = OUT / "diagtmp"
    worker.tmp_dir.mkdir(parents=True, exist_ok=True)

    total = plan.total_duration
    done = 0.0
    parts = []
    for i, piece in enumerate(plan.pieces):
        part = worker.tmp_dir / f"p{i:04d}.ts"
        worker._run_piece(piece, plan, part, done, total, f"parte {i}")
        done += piece.out_duration
        got = frames(str(part))
        want = round(piece.out_duration * 30)
        flag = "  <-- SOBRAN" if got > want else ("  <-- FALTAN" if got < want else "")
        print(f"pieza {i} {piece.mode:6s} [{piece.start:7.3f} -> {piece.end:7.3f}] "
              f"dur={piece.out_duration:6.3f}  esperados={want:4d}  reales={got:4d}{flag}")
        parts.append(part)

    print(f"\nSuma de fotogramas de los trozos: "
          f"{sum(frames(str(p)) for p in parts)}")

    audio = worker._build_audio(plan)
    print(f"Audio generado: {stream_durations(str(audio))}")

    worker._concat(parts, plan, audio)
    final = str(OUT / "diag.mp4")
    print(f"\nFinal: {frames(final)} fotogramas")
    print(f"Duraciones del final: {stream_durations(final)}")


if __name__ == "__main__":
    main()
