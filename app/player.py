"""Previsualizacion con libmpv embebido en un widget de Qt.

Por que mpv y no QtMultimedia: mpv reproduce una EDL (lista de edicion) de forma
nativa, asi que la linea de tiempo completa se ve como un solo video continuo,
sin saltos al pasar de un clip a otro, y decodifica en la GPU sin cargar el
archivo en memoria. Un MP4 de 20 GB abre igual de rapido que uno pequenio.
"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import QWidget

from .ffmpeg_tools import BIN, CACHE
from .model import Timeline

# Carga de libmpv-2.dll desde bin/.
#
# python-mpv localiza la DLL con ctypes.util.find_library, que en Windows
# recorre %PATH%. Eso obliga a que bin/ este en PATH durante el import, pero
# solo durante el import: si se deja puesto, cada ffmpeg que lancemos despues
# hereda un PATH cuyo primer elemento es una carpeta de la aplicacion, y
# cualquier ejecutable que aparezca ahi pasa a ser el candidato preferido para
# resolver nombres de programa.
#
# Poner bin/ el primero tambien garantiza que se carga NUESTRA copia y no otra
# libmpv-2.dll que estuviera antes en el PATH del sistema.
_saved_path = os.environ.get("PATH", "")
os.environ["PATH"] = str(BIN) + os.pathsep + _saved_path
if hasattr(os, "add_dll_directory") and BIN.is_dir():
    try:
        os.add_dll_directory(str(BIN))
    except OSError:
        pass

try:
    import mpv  # noqa: E402  (necesita el PATH ya ajustado)
finally:
    # La DLL ya esta cargada en el proceso; el PATH ampliado deja de hacer falta.
    os.environ["PATH"] = _saved_path


def _q(text: str) -> str:
    """Comillas de mpv por longitud en bytes: sobrevive a rutas con , ; % o acentos."""
    raw = text.encode("utf-8")
    return f"%{len(raw)}%{text}"


def build_edl(timeline: Timeline) -> str:
    """Genera una EDL de mpv que representa la linea de tiempo completa."""
    lines = ["# mpv EDL v0"]
    for clip in timeline.clips:
        if clip.duration <= 0:
            continue
        lines.append(f"{_q(clip.source.path)},{clip.in_t:.6f},{clip.duration:.6f}")
    return "\n".join(lines) + "\n"


class PlayerWidget(QWidget):
    """Superficie de video + control de reproduccion sobre la linea de tiempo."""

    position_changed = Signal(float)     # tiempo en la linea de tiempo, en segundos
    playing_changed = Signal(bool)
    error = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WA_NativeWindow)
        # mpv pinta directamente sobre la ventana nativa: si Qt rellenara el fondo
        # encima, borraria cada fotograma.
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setMinimumHeight(220)

        self.mpv = None
        self._timeline: Timeline | None = None
        self._loaded_edl = ""
        self._position = 0.0
        self._duration = 0.0
        self._current_clip = -1
        self._suppress_pos = False
        self._volume = 100
        self._muted = False

        # La rotacion se aplica por clip, asi que hay que revisarla al avanzar.
        self._tick = QTimer(self)
        self._tick.setInterval(120)
        self._tick.timeout.connect(self._sync_rotation)

    def showEvent(self, event):  # pragma: no cover - Qt
        # mpv se engancha al HWND del widget, y ese handle solo es definitivo una
        # vez que el widget esta dentro de su jerarquia final y visible. Crearlo
        # en el constructor deja a mpv pintando sobre una ventana que ya no existe.
        super().showEvent(event)
        if self.mpv is None:
            self._init_mpv()

    def _init_mpv(self) -> None:
        self.mpv = mpv.MPV(
            wid=str(int(self.winId())),
            vo="gpu-next",
            gpu_api="d3d11",
            hwdec="auto-safe",          # NVDEC/D3D11VA: decodifica en la GPU
            hr_seek="yes",              # seek exacto al fotograma
            hr_seek_framedrop="no",
            keep_open="always",
            idle=True,
            osc=False,
            border=False,
            input_default_bindings=False,
            input_vo_keyboard=False,
            cache="yes",
            demuxer_max_bytes="256MiB",
            demuxer_readahead_secs=20,
            audio_display="no",
            terminal=False,
            # Endurecimiento explicito en vez de confiar en los valores por
            # defecto de libmpv: sin configuracion del usuario, sin scripts de
            # Lua/JS y sin ytdl. Este reproductor solo tiene que abrir archivos
            # locales, asi que no hay motivo para dejarle cargar codigo ni
            # tocar la red por su cuenta.
            config=False,
            load_scripts=False,
            ytdl=False,
            osd_level=0,
        )

        # mpv notifica desde su propio hilo; las senales de Qt cruzan al hilo de UI.
        @self.mpv.property_observer("time-pos")
        def _on_time(_name, value):  # pragma: no cover - callback de mpv
            if value is not None and not self._suppress_pos:
                self.position_changed.emit(float(value))

        @self.mpv.property_observer("pause")
        def _on_pause(_name, value):  # pragma: no cover - callback de mpv
            self.playing_changed.emit(not bool(value))

        self.position_changed.connect(self._track_clip)
        self.set_volume(self._volume)
        self.set_muted(self._muted)
        self._tick.start()

        # Si se importo un video antes de que la ventana llegara a mostrarse,
        # ese timeline quedo pendiente de cargar.
        if self._timeline is not None and self._timeline.clips:
            self._loaded_edl = ""
            self.set_timeline(self._timeline, keep_position=False)

    # ---------- carga ----------

    def set_timeline(self, timeline: Timeline, keep_position: bool = True) -> None:
        """Reconstruye la EDL. Solo recarga mpv si el contenido cambio de verdad."""
        self._timeline = timeline
        self._duration = timeline.duration
        if self.mpv is None:
            return   # todavia no visible; se cargara en showEvent

        if not timeline.clips:
            self._loaded_edl = ""
            self._current_clip = -1
            try:
                self.mpv.command("stop")
            except Exception:
                pass
            self.position_changed.emit(0.0)
            return

        edl = build_edl(timeline)
        if edl == self._loaded_edl:
            return
        self._loaded_edl = edl

        CACHE.mkdir(parents=True, exist_ok=True)
        edl_path = CACHE / "preview.edl"
        # newline="\n" es obligatorio: en Windows write_text convertiria los saltos
        # a CRLF y el parser de EDL de mpv rechaza la cabecera con el retorno de
        # carro, dejando la previsualizacion en negro sin dar ningun error.
        edl_path.write_text(edl, encoding="utf-8", newline="\n")

        target = min(self._position, max(0.0, self._duration - 0.05)) if keep_position else 0.0
        was_playing = self.is_playing()
        try:
            self.mpv.command("loadfile", str(edl_path), "replace")
            self.mpv.pause = True
            # El seek tiene que esperar a que el archivo este cargado.
            QTimer.singleShot(90, lambda: self._after_load(target, was_playing))
        except Exception as exc:  # pragma: no cover - depende de mpv
            self.error.emit(f"No se pudo cargar la previsualizacion: {exc}")

    def _after_load(self, target: float, resume: bool) -> None:
        if self.mpv is None:
            return
        self.seek(target)
        self._current_clip = -1
        self._sync_rotation()
        if resume:
            self.mpv.pause = False

    # ---------- transporte ----------

    def is_playing(self) -> bool:
        if self.mpv is None:
            return False
        try:
            return not bool(self.mpv.pause)
        except Exception:
            return False

    def play(self) -> None:
        if self.mpv is not None and self._timeline and self._timeline.clips:
            self.mpv.pause = False

    def pause(self) -> None:
        if self.mpv is None:
            return
        try:
            self.mpv.pause = True
        except Exception:
            pass

    def toggle(self) -> None:
        if self.is_playing():
            self.pause()
        else:
            self.play()

    def seek(self, t: float) -> None:
        if self.mpv is None or not self._timeline or not self._timeline.clips:
            return
        t = max(0.0, min(t, max(0.0, self._duration - 0.001)))
        self._position = t
        try:
            self.mpv.command("seek", f"{t:.6f}", "absolute+exact")
        except Exception:
            pass

    def step(self, frames: int = 1) -> None:
        """Avanza o retrocede fotograma a fotograma."""
        if self.mpv is None or not self._timeline or not self._timeline.clips:
            return
        self.pause()
        cmd = "frame-step" if frames > 0 else "frame-back-step"
        for _ in range(abs(frames)):
            try:
                self.mpv.command(cmd)
            except Exception:
                break

    def nudge(self, seconds: float) -> None:
        self.seek(self._position + seconds)

    def set_volume(self, value: int) -> None:
        self._volume = max(0, min(130, int(value)))
        if self.mpv is None:
            return
        try:
            self.mpv.volume = self._volume
        except Exception:
            pass

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        if self.mpv is None:
            return
        try:
            self.mpv.mute = self._muted
        except Exception:
            pass

    # ---------- rotacion por clip ----------

    def _track_clip(self, t: float) -> None:
        self._position = t

    def _sync_rotation(self) -> None:
        """mpv rota el video entero, no por segmento: se ajusta al cruzar de clip."""
        if self.mpv is None or not self._timeline or not self._timeline.clips:
            return
        idx, _ = self._timeline.clip_at(self._position)
        if idx is None or idx == self._current_clip:
            return
        self._current_clip = idx
        try:
            self.mpv.video_rotate = self._timeline.clips[idx].effective_rotation
        except Exception:
            pass

    def refresh_rotation(self) -> None:
        """Forzar reevaluacion tras rotar el clip que se esta viendo."""
        self._current_clip = -1
        self._sync_rotation()

    # ---------- ciclo de vida ----------

    def shutdown(self) -> None:
        self._tick.stop()
        if self.mpv is None:
            return
        try:
            self.mpv.terminate()
        except Exception:
            pass
        self.mpv = None

    def closeEvent(self, event):  # pragma: no cover - Qt
        self.shutdown()
        super().closeEvent(event)
