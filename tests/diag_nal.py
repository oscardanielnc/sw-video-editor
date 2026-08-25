"""Comprueba a nivel de bytes si un trozo lleva sus cabeceras SPS/PPS dentro."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ffmpeg_tools import FFMPEG, run  # noqa: E402

SAMPLES = Path(__file__).resolve().parent / "samples"
OUT = Path(__file__).resolve().parent / "out" / "nal"
SRC = str(SAMPLES / "realistic_90s.mp4")
KF = 50.05


def count_nals(path: Path) -> tuple[int, int]:
    """Cuenta arranques Annex-B de SPS (tipo 7) y PPS (tipo 8)."""
    data = path.read_bytes()
    sps = pps = 0
    i = 0
    while True:
        i = data.find(b"\x00\x00\x01", i)
        if i < 0:
            break
        nal = data[i + 3] & 0x1F if i + 3 < len(data) else 0
        if nal == 7:
            sps += 1
        elif nal == 8:
            pps += 1
        i += 3
    return sps, pps


def make(name: str, fmt: str, ext: str, bsf: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"{name}{ext}"
    res = run([FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-ss", f"{KF:.6f}", "-i", SRC, "-frames:v", "120",
               "-map", "0:v:0", "-c:v", "copy", "-an"] + bsf +
              ["-avoid_negative_ts", "make_zero", "-f", fmt, "-y", str(out)],
              timeout=300)
    if res.returncode != 0:
        print(f"  {name:26s} -> ffmpeg fallo: {(res.stderr or '')[-70:]}")
        return
    sps, pps = count_nals(out)
    dec = run([FFMPEG, "-hide_banner", "-v", "error", "-nostdin",
               "-i", str(out), "-f", "null", "-"], timeout=300)
    errs = len([x for x in (dec.stderr or "").splitlines() if x.strip()])
    print(f"  {name:26s} -> SPS={sps:3d}  PPS={pps:3d}  errores={errs}")


def main() -> None:
    print(f"Trozo copiado desde el keyframe {KF} s (120 fotogramas)\n")
    make("ts_sin_bsf", "mpegts", ".ts", [])
    make("ts_annexb", "mpegts", ".ts", ["-bsf:v", "h264_mp4toannexb"])
    make("mkv", "matroska", ".mkv", [])
    print("\nFuente original (referencia):")
    sps, pps = count_nals(Path(SRC))
    print(f"  {'mp4 original':26s} -> SPS={sps:3d}  PPS={pps:3d}")


if __name__ == "__main__":
    main()
