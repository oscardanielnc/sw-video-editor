"""Prueba de humo de la interfaz: abre la ventana real y encadena ediciones.

Lanza mpv de verdad y ejecuta las mismas acciones que dispararian los botones,
para detectar fallos de conexion de senales que las pruebas del nucleo no ven.
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPoint, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ffmpeg_tools import CACHE  # noqa: E402
from app.main_window import MainWindow  # noqa: E402
from app.player import build_edl  # noqa: E402
from app.timeline_widget import TRACK_TOP  # noqa: E402

SAMPLES = Path(__file__).resolve().parent / "samples"
LAND60 = str(SAMPLES / "landscape_60s.mp4")
LAND30 = str(SAMPLES / "landscape_30s.mp4")

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


def click(view, x: int, y: int) -> None:
    """Simula un clic completo sobre la linea de tiempo."""
    pos = QPoint(x, y)
    for kind in (QMouseEvent.Type.MouseButtonPress, QMouseEvent.Type.MouseButtonRelease):
        ev = QMouseEvent(kind, pos, view.mapToGlobal(pos), Qt.LeftButton,
                         Qt.LeftButton if kind == QMouseEvent.Type.MouseButtonPress
                         else Qt.NoButton, Qt.NoModifier)
        QApplication.sendEvent(view, ev)


def spin(app: QApplication, ms: int) -> None:
    """Deja correr el bucle de eventos: mpv carga de forma asincrona."""
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


def run_checks(win: MainWindow, app: QApplication) -> None:
    tl, view = win.timeline, win.view

    print("\n[1] Importar")
    win.add_files([LAND60])
    app.processEvents()
    check("un clip en la linea de tiempo", len(tl.clips) == 1, str(len(tl.clips)))
    check("duracion visible 60 s", abs(tl.duration - 60) < 0.5, f"{tl.duration}")
    check("la EDL de mpv se genero", build_edl(tl).startswith("# mpv EDL v0"),
          "cabecera incorrecta")
    check("el zoom encaja el contenido", view.pps > 0, f"pps={view.pps}")

    print("\n[1b] mpv carga la EDL de verdad")
    # Regresion: el parser de EDL de mpv rechaza los saltos de linea CRLF y falla
    # en silencio, dejando la previsualizacion en negro sin ningun error visible.
    edl_file = CACHE / "preview.edl"
    check("la EDL se escribio en disco", edl_file.exists(), "no existe")
    if edl_file.exists():
        raw = edl_file.read_bytes()
        check("la EDL no lleva retornos de carro", b"\r" not in raw,
              "contiene CRLF: mpv la rechazaria")

    spin(app, 2500)
    m = win.player.mpv
    check("mpv tiene un archivo cargado", m.filename is not None, "ninguno")
    check("mpv reporta 60 s", m.duration is not None and abs(m.duration - 60) < 1.0,
          str(m.duration))
    check("la salida de video esta configurada", bool(m.vo_configured), "sin vo")
    check("decodifica por GPU", bool(m.hwdec_current) and m.hwdec_current != "no",
          str(m.hwdec_current))
    check("resolucion detectada", (m.width, m.height) == (1280, 720),
          f"{m.width}x{m.height}")

    print("\n[2] Seleccion con el raton")
    click(view.viewport() if hasattr(view, "viewport") else view,
          view.width() // 2, TRACK_TOP + 40)
    app.processEvents()
    check("clic sobre el clip lo selecciona", view.selected_cid == tl.clips[0].cid,
          f"cid={view.selected_cid}")

    print("\n[3] Dividir y eliminar")
    view.set_playhead(20.0, follow=False)
    win.split_here()
    app.processEvents()
    check("dividir crea 2 clips", len(tl.clips) == 2, str(len(tl.clips)))
    check("queda seleccionado el trozo derecho",
          view.selected_cid == tl.clips[1].cid, f"cid={view.selected_cid}")

    win.delete_selected()
    app.processEvents()
    check("eliminar deja 1 clip", len(tl.clips) == 1, str(len(tl.clips)))
    check("duracion baja a 20 s", abs(tl.duration - 20) < 0.5, f"{tl.duration}")
    check("boton eliminar se desactiva sin seleccion",
          not win.act_delete.isEnabled(), "sigue activo")

    print("\n[4] Deshacer y rehacer")
    win.undo()
    app.processEvents()
    check("deshacer recupera el clip", len(tl.clips) == 2, str(len(tl.clips)))
    win.undo()
    app.processEvents()
    check("deshacer otra vez deja 1 clip entero", len(tl.clips) == 1,
          str(len(tl.clips)))
    check("duracion vuelve a 60 s", abs(tl.duration - 60) < 0.5, f"{tl.duration}")
    win.redo()
    app.processEvents()
    check("rehacer vuelve a 2 clips", len(tl.clips) == 2, str(len(tl.clips)))

    print("\n[5] Rotacion")
    view.select(tl.clips[0].cid)
    win.rotate_selected(90)
    app.processEvents()
    check("clip rotado a 90", tl.clips[0].rotate == 90, str(tl.clips[0].rotate))
    check("mpv recibio la rotacion", win.player.mpv.video_rotate in (90, "90"),
          str(win.player.mpv.video_rotate))
    win.rotate_all()
    app.processEvents()
    check("girar todo suma 90 a cada clip",
          [c.rotate for c in tl.clips] == [180, 90],
          str([c.rotate for c in tl.clips]))

    print("\n[6] Reordenar y segundo video")
    win.add_files([LAND30])
    app.processEvents()
    check("tres clips tras importar el segundo", len(tl.clips) == 3,
          str(len(tl.clips)))
    order_before = [c.cid for c in tl.clips]
    tl.move(order_before[-1], 0)
    win._on_edited("Mover clip")
    app.processEvents()
    check("el ultimo clip pasa al principio", tl.clips[0].cid == order_before[-1],
          "no se movio")
    check("se conserva el numero de clips", len(tl.clips) == 3, str(len(tl.clips)))

    print("\n[7] Transporte y estado")
    win.player.seek(5.0)
    app.processEvents()
    check("el reloj se actualiza", ":" in win.lbl_time.text(), win.lbl_time.text())
    check("exportar disponible con clips", win.act_export.isEnabled(), "desactivado")
    view.zoom_to_fit()
    check("ajustar zoom no rompe nada", view.pps > 0, f"pps={view.pps}")

    print("\n[8] Guardar y abrir proyecto")
    prj = Path(__file__).resolve().parent / "out" / "smoke.swproj"
    prj.parent.mkdir(parents=True, exist_ok=True)
    tl.save(str(prj))
    expected = [(c.source.path, round(c.in_t, 3), round(c.out_t, 3), c.rotate)
                for c in tl.clips]
    missing = tl.load(str(prj))
    got = [(c.source.path, round(c.in_t, 3), round(c.out_t, 3), c.rotate)
           for c in tl.clips]
    check("el proyecto se recarga identico", got == expected, f"{got} != {expected}")
    check("no faltan archivos", not missing, str(missing))


def main() -> int:
    if not Path(LAND60).exists():
        print("Faltan los videos de prueba: ejecuta antes tests/make_samples.py")
        return 2

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    app.processEvents()

    code = 0
    try:
        run_checks(win, app)
    except Exception:
        traceback.print_exc()
        _fails.append("excepcion no controlada")

    print("\n" + "=" * 64)
    print(f"{_passes} comprobaciones correctas, {len(_fails)} fallos")
    for f in _fails:
        print("  FAIL " + f)
    code = 1 if _fails else 0

    win.view.thumbs.shutdown()
    win.player.shutdown()
    QTimer.singleShot(0, app.quit)
    app.exec()
    return code


if __name__ == "__main__":
    sys.exit(main())
