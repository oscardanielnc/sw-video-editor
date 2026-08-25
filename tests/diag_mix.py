"""Busca una estrategia de union que tolere mezclar trozos copiados y recodificados."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ffmpeg_tools import FFMPEG, run  # noqa: E402

SAMPLES = Path(__file__).resolve().parent / "samples"
OUT = Path(__file__).resolve().parent / "out" / "mix"
SRC = str(SAMPLES / "realistic_90s.mp4")
FPS = 30000 / 1001

# Un fragmento recodificado seguido de un tramo copiado que arranca en keyframe.
ENC_FROM, ENC_FRAMES = 45.70, 130      # 45.70 -> 50.05
CPY_FROM, CPY_FRAMES = 50.05, 300


def dec_errors(path: Path) -> tuple[int, str]:
    res = run([FFMPEG, "-hide_banner", "-v", "error", "-nostdin",
               "-i", str(path), "-f", "null", "-"], timeout=600)
    lines = [x for x in (res.stderr or "").splitlines() if x.strip()]
    return len(lines), (lines[0][:90] if lines else "")


def build(name: str, fmt: str, ext: str, piece_bsf: list[str],
          out_container: str, out_ext: str, concat_extra: list[str]) -> None:
    # El nombre lleva '>' y espacios, que no valen como carpeta en Windows.
    d = OUT / name.split(".")[0]
    d.mkdir(parents=True, exist_ok=True)

    enc = d / f"enc{ext}"
    run([FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
         "-noautorotate", "-ss", f"{ENC_FROM:.6f}", "-i", SRC,
         "-frames:v", str(ENC_FRAMES), "-map", "0:v:0",
         "-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq",
         "-rc", "constqp", "-qp", "18", "-profile:v", "high",
         "-pix_fmt", "yuv420p", "-bf", "2", "-r", f"{FPS:.6f}", "-an"] +
        piece_bsf + ["-f", fmt, "-y", str(enc)], timeout=600)

    cpy = d / f"cpy{ext}"
    run([FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
         "-ss", f"{CPY_FROM:.6f}", "-i", SRC,
         "-frames:v", str(CPY_FRAMES), "-map", "0:v:0", "-c:v", "copy", "-an",
         "-avoid_negative_ts", "make_zero"] +
        piece_bsf + ["-f", fmt, "-y", str(cpy)], timeout=600)

    for label, part in (("fragmento", enc), ("copia", cpy)):
        if not part.exists() or part.stat().st_size == 0:
            print(f"  {name:34s} -> no se genero el {label}")
            return

    listing = d / "list.txt"
    listing.write_text(f"file '{enc.as_posix()}'\nfile '{cpy.as_posix()}'\n",
                       encoding="utf-8", newline="\n")
    final = d / f"final{out_ext}"
    res = run([FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-f", "concat", "-safe", "0", "-fflags", "+genpts",
               "-i", str(listing), "-map", "0:v:0", "-c", "copy"] +
              concat_extra + ["-y", str(final)], timeout=600)
    if res.returncode != 0 or not final.exists():
        print(f"  {name:34s} -> fallo al unir: {(res.stderr or '')[-80:]}")
        return

    n_enc, _ = dec_errors(enc)
    n_cpy, _ = dec_errors(cpy)
    n, first = dec_errors(final)
    mark = "CORRECTO" if n <= 2 else "ROTO"
    print(f"  {name:34s} -> {mark:8s} trozos({n_enc},{n_cpy})  final={n:5d}  {first}")


def main() -> None:
    print("Fragmento NVENC + tramo copiado, unidos y decodificados:\n")

    build("1. MKV -> MP4 (actual)", "matroska", ".mkv", [],
          "mp4", ".mp4", ["-movflags", "+faststart"])
    build("2. TS annexb -> MP4", "mpegts", ".ts",
          ["-bsf:v", "h264_mp4toannexb"], "mp4", ".mp4",
          ["-movflags", "+faststart"])
    build("3. TS annexb -> MKV", "mpegts", ".ts",
          ["-bsf:v", "h264_mp4toannexb"], "matroska", ".mkv", [])
    build("4. TS sin bsf -> MP4", "mpegts", ".ts", [],
          "mp4", ".mp4", ["-movflags", "+faststart"])
    build("5. MKV -> MKV", "matroska", ".mkv", [], "matroska", ".mkv", [])
    build("6. MP4 -> MP4", "mp4", ".mp4", [], "mp4", ".mp4",
          ["-movflags", "+faststart"])


if __name__ == "__main__":
    main()
