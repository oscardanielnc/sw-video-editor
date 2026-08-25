"""Punto de entrada del editor."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from .ffmpeg_tools import verify_binaries

NEWLINE = chr(10)


def _check_binaries() -> str:
    """Nada arranca hasta que bin/ existe Y coincide con los hashes publicados.

    Se comprueba antes de importar el reproductor porque ese import carga
    libmpv-2.dll: verificar despues seria verificar codigo que ya se ejecuto.
    """
    problems = verify_binaries()
    if not problems:
        return ""
    return ("Los binarios de bin/ no estan como deberian:" + NEWLINE + "  - " +
            (NEWLINE + "  - ").join(problems) +
            NEWLINE + NEWLINE +
            "El README explica de donde se descarga cada uno y como "
            "comprobarlos.")


def main() -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("SW Video Editor")

    problema = _check_binaries()
    if problema:
        QMessageBox.critical(None, "Instalacion incompleta", problema)
        return 1

    from .main_window import MainWindow  # importa mpv, asi que va tras la comprobacion

    win = MainWindow()
    win.show()

    # Permite abrir arrastrando archivos sobre el .bat o pasandolos por consola.
    archivos = [a for a in sys.argv[1:] if Path(a).is_file()]
    if archivos:
        win.add_files(archivos)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
