"""Abre el dialogo de exportacion con un proyecto de ejemplo, para revisarlo a ojo."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.main_window import ExportDialog  # noqa: E402
from app.model import Timeline  # noqa: E402

SAMPLES = Path(__file__).resolve().parent / "samples"


def main() -> int:
    app = QApplication(sys.argv)
    tl = Timeline()
    tl.append_source(str(SAMPLES / "realistic_90s.mp4"))
    tl.split_at(15.3)
    tl.split_at(45.7)
    tl.delete(tl.clips[1].cid)

    dlg = ExportDialog(tl)
    dlg.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
