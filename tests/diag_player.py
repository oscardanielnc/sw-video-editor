"""Diagnostico del reproductor: confirma que mpv decodifica y pinta de verdad."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.main_window import MainWindow  # noqa: E402

SAMPLES = Path(__file__).resolve().parent / "samples"
OUT = Path(__file__).resolve().parent / "out"


def report(win: MainWindow) -> None:
    m = win.player.mpv
    print(f"instancia mpv creada : {m is not None}")
    if m is None:
        return
    for prop in ("filename", "duration", "time-pos", "pause", "width", "height",
                 "dwidth", "dheight", "video-codec", "hwdec-current",
                 "current-vo", "gpu-api", "video-rotate", "estimated-vf-fps",
                 "vo-configured", "container-fps"):
        try:
            print(f"  {prop:20s}= {m._get_property(prop)}")
        except Exception as exc:
            print(f"  {prop:20s}! {exc}")

    # Si mpv puede volcar el fotograma actual a PNG, la decodificacion y la
    # cadena de render funcionan aunque la captura de pantalla salga negra.
    shot = OUT / "mpv_frame.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    shot.unlink(missing_ok=True)
    try:
        m.command("screenshot-to-file", str(shot), "video")
        ok = shot.exists() and shot.stat().st_size > 0
        print(f"\nfotograma volcado por mpv: {ok} "
              f"({shot.stat().st_size if ok else 0} bytes) -> {shot}")
    except Exception as exc:
        print(f"\nno se pudo volcar el fotograma: {exc}")


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    app.processEvents()
    win.add_files([str(SAMPLES / "landscape_60s.mp4")])

    def finish() -> None:
        win.player.seek(12.0)
        QTimer.singleShot(1500, lambda: (report(win), app.quit()))

    QTimer.singleShot(3000, finish)
    app.exec()
    win.view.thumbs.shutdown()
    win.player.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
