"""Ventana principal: previsualizacion, transporte, linea de tiempo y exportacion."""
from __future__ import annotations

import html
import os
import subprocess
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                               QProgressBar, QPushButton, QSizePolicy, QSlider,
                               QSplitter, QStatusBar, QToolBar, QVBoxLayout,
                               QWidget)

from .exporter import MODE_EXACT, MODE_LOSSLESS, ExportWorker, build_plan
from .ffmpeg_tools import human_size, timecode
from .model import History, Timeline
from .player import PlayerWidget
from .timeline_widget import TimelinePanel

NEWLINE = chr(10)

VIDEO_FILTER = ("Video (*.mp4 *.mov *.mkv *.avi *.m4v *.mts *.m2ts *.webm *.wmv "
                "*.flv);;Todos los archivos (*.*)")


class ExportDialog(QDialog):
    """Opciones de exportacion, con el resumen de lo que se copiara sin recodificar."""

    def __init__(self, timeline: Timeline, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Exportar video")
        self.setMinimumWidth(560)
        self.tl = timeline
        self.out_path = ""

        self.path_label = QLabel("<i>Sin destino elegido</i>")
        self.path_label.setWordWrap(True)
        browse = QPushButton("Elegir destino...")
        browse.clicked.connect(self._pick)

        row = QHBoxLayout()
        row.addWidget(self.path_label, 1)
        row.addWidget(browse)

        self.mode = QComboBox()
        self.mode.addItem("Sin recodificar · copia bit a bit (recomendado)",
                          MODE_LOSSLESS)
        self.mode.addItem("Exacto al fotograma · recodifica todo", MODE_EXACT)
        self.mode.currentIndexChanged.connect(self._analyze)

        self.encoder = QComboBox()
        self.encoder.addItem("NVENC · GPU NVIDIA (recomendado)", "h264_nvenc")
        self.encoder.addItem("HEVC NVENC · GPU NVIDIA", "hevc_nvenc")
        self.encoder.addItem("libx264 · CPU", "libx264")
        self.quality = QComboBox()
        self.quality.addItem("Ajustar al original (tamanio parecido)", 0)
        self.quality.addItem("Muy alta (QP 18 · archivo ~2x mas grande)", 18)
        self.quality.addItem("Maxima (QP 16 · archivo mucho mas grande)", 16)
        self.quality.addItem("Compacta (QP 24)", 24)

        self.summary = QLabel("Calculando...")
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(
            "background:#1c2530; padding:10px; border-radius:6px; color:#cfe0f0;")

        form = QFormLayout()
        form.addRow("Destino:", self._wrap(row))
        form.addRow("Modo:", self.mode)
        form.addRow("Codificador:", self.encoder)
        form.addRow("Calidad:", self.quality)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Exportar")
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(QLabel("<b>Que va a pasar</b>"))
        lay.addWidget(self.summary)
        lay.addWidget(self.buttons)

        # El analisis de keyframes lanza ffprobe, asi que se hace tras mostrar el
        # dialogo para que la ventana no aparezca congelada.
        QTimer.singleShot(50, self._analyze)

    @staticmethod
    def _wrap(layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w

    def _analyze(self) -> None:
        QApplication.setOverrideCursor(Qt.BusyCursor)
        try:
            mode = str(self.mode.currentData())
            plan = build_plan(self.tl, mode)
            # Los ajustes de codificacion solo pintan si se va a recodificar.
            reencoding = plan.mode == MODE_EXACT
            self.encoder.setEnabled(reencoding)
            self.quality.setEnabled(reencoding)

            # Todo este texto lleva dentro nombres de archivo, que son datos
            # de fuera. QLabel interpreta HTML, asi que un video llamado
            # '<img src=...>.mp4' conseguiria que el dialogo cargue lo que diga
            # el nombre. Se escapa el contenido y solo se anade marcado propio.
            text = html.escape(plan.summary())
            detail = plan.shift_detail()
            if detail:
                text += ("<br><br><b>Desplazamiento de cada corte:</b><br>" +
                         "<br>".join("· " + html.escape(d) for d in detail))
            if plan.warnings:
                text += "<br><br>" + "<br>".join(
                    "· " + html.escape(w) for w in plan.warnings)
            self.summary.setText(text.replace(NEWLINE, "<br>"))
        except Exception as exc:
            self.summary.setText(
                html.escape(f"No se pudo analizar el proyecto: {exc}"))
        finally:
            QApplication.restoreOverrideCursor()

    def _pick(self) -> None:
        default = ""
        if self.tl.clips:
            src = Path(self.tl.clips[0].source.path)
            default = str(src.with_name(src.stem + "_editado.mp4"))
        path, _ = QFileDialog.getSaveFileName(self, "Guardar video como", default,
                                              "Video MP4 (*.mp4)")
        if path:
            if not path.lower().endswith(".mp4"):
                path += ".mp4"
            self.out_path = path
            self.path_label.setText(path)
            self.buttons.button(QDialogButtonBox.Ok).setEnabled(True)


class ProgressDialog(QDialog):
    """Progreso de exportacion, cancelable."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Exportando")
        self.setMinimumWidth(460)
        self.setWindowFlag(Qt.WindowCloseButtonHint, False)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.label = QLabel("Preparando...")
        self.label.setWordWrap(True)
        self.cancel_btn = QPushButton("Cancelar")

        lay = QVBoxLayout(self)
        lay.addWidget(self.label)
        lay.addWidget(self.bar)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.cancel_btn)
        lay.addLayout(row)

    def update_progress(self, frac: float, message: str) -> None:
        self.bar.setValue(int(max(0.0, min(1.0, frac)) * 1000))
        self.label.setText(message)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("SW · Editor de video")
        self.resize(1400, 900)
        self.setAcceptDrops(True)

        self.settings = QSettings("sw", "video-editor")
        self.timeline = Timeline()
        self.history = History(self.timeline)
        self.worker: ExportWorker | None = None
        self.progress_dlg: ProgressDialog | None = None

        self.player = PlayerWidget(self)
        self.panel = TimelinePanel(self.timeline, self)
        self.view = self.panel.view

        self._build_ui()
        self._connect()
        self._apply_theme()
        self._update_state()

    # ---------- construccion ----------

    def _build_ui(self) -> None:
        tb = QToolBar("Principal")
        tb.setMovable(False)
        self.addToolBar(tb)

        self.act_import = QAction("Importar video", self)
        self.act_import.setShortcut(QKeySequence.Open)
        self.act_split = QAction("Dividir  (S)", self)
        self.act_delete = QAction("Eliminar clip  (Supr)", self)
        self.act_rot_l = QAction("Girar 90° izq.", self)
        self.act_rot_r = QAction("Girar 90° der.  (R)", self)
        self.act_rot_all = QAction("Girar TODO 90°", self)
        self.act_undo = QAction("Deshacer", self)
        self.act_undo.setShortcut(QKeySequence.Undo)
        self.act_redo = QAction("Rehacer", self)
        self.act_redo.setShortcut(QKeySequence.Redo)
        self.act_export = QAction("Exportar...", self)
        self.act_export.setShortcut("Ctrl+E")
        self.act_open_prj = QAction("Abrir proyecto", self)
        self.act_save_prj = QAction("Guardar proyecto", self)
        self.act_save_prj.setShortcut(QKeySequence.Save)

        for a in (self.act_import, None, self.act_split, self.act_delete, None,
                  self.act_rot_l, self.act_rot_r, self.act_rot_all, None,
                  self.act_undo, self.act_redo, None,
                  self.act_open_prj, self.act_save_prj):
            tb.addSeparator() if a is None else tb.addAction(a)
        # Empuja el boton de exportar al extremo derecho de la barra.
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        tb.addAction(self.act_export)

        # --- transporte ---
        self.btn_play = QPushButton("▶")
        self.btn_play.setFixedWidth(52)
        self.btn_prev = QPushButton("◀|")
        self.btn_next = QPushButton("|▶")
        for b in (self.btn_prev, self.btn_next):
            b.setFixedWidth(44)
        self.lbl_time = QLabel("00:00:00:00 / 00:00:00:00")
        self.lbl_time.setStyleSheet("font-family: Consolas, monospace; color:#dfe6f0;")

        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 130)
        self.vol.setValue(int(self.settings.value("volume", 100)))
        self.vol.setFixedWidth(110)

        self.btn_zoom_out = QPushButton("−")
        self.btn_zoom_in = QPushButton("+")
        self.btn_fit = QPushButton("Ajustar")
        for b in (self.btn_zoom_out, self.btn_zoom_in):
            b.setFixedWidth(34)

        bar = QHBoxLayout()
        bar.setContentsMargins(8, 4, 8, 4)
        for w in (self.btn_prev, self.btn_play, self.btn_next):
            bar.addWidget(w)
        bar.addSpacing(12)
        bar.addWidget(self.lbl_time)
        bar.addStretch(1)
        bar.addWidget(QLabel("Vol"))
        bar.addWidget(self.vol)
        bar.addSpacing(12)
        bar.addWidget(QLabel("Zoom"))
        bar.addWidget(self.btn_zoom_out)
        bar.addWidget(self.btn_zoom_in)
        bar.addWidget(self.btn_fit)
        bar_w = QWidget()
        bar_w.setLayout(bar)

        bottom = QWidget()
        blay = QVBoxLayout(bottom)
        blay.setContentsMargins(0, 0, 0, 0)
        blay.setSpacing(0)
        blay.addWidget(bar_w)
        blay.addWidget(self.panel)

        split = QSplitter(Qt.Vertical)
        split.addWidget(self.player)
        split.addWidget(bottom)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setCollapsible(0, False)
        split.setCollapsible(1, False)
        self.setCentralWidget(split)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Importa un video para empezar (Ctrl+O)")

    def _connect(self) -> None:
        self.act_import.triggered.connect(self.import_video)
        self.act_split.triggered.connect(self.split_here)
        self.act_delete.triggered.connect(self.delete_selected)
        self.act_rot_l.triggered.connect(lambda: self.rotate_selected(-90))
        self.act_rot_r.triggered.connect(lambda: self.rotate_selected(90))
        self.act_rot_all.triggered.connect(self.rotate_all)
        self.act_undo.triggered.connect(self.undo)
        self.act_redo.triggered.connect(self.redo)
        self.act_export.triggered.connect(self.export)
        self.act_open_prj.triggered.connect(self.open_project)
        self.act_save_prj.triggered.connect(self.save_project)

        self.btn_play.clicked.connect(self.player.toggle)
        self.btn_prev.clicked.connect(lambda: self.player.step(-1))
        self.btn_next.clicked.connect(lambda: self.player.step(1))
        self.vol.valueChanged.connect(self._on_volume)
        self.btn_zoom_in.clicked.connect(lambda: self.view.set_zoom(self.view.pps * 1.4))
        self.btn_zoom_out.clicked.connect(lambda: self.view.set_zoom(self.view.pps / 1.4))
        self.btn_fit.clicked.connect(self.view.zoom_to_fit)

        self.player.position_changed.connect(self._on_position)
        self.player.playing_changed.connect(
            lambda p: self.btn_play.setText("❚❚" if p else "▶"))
        self.player.error.connect(
            lambda m: self.statusBar().showMessage(m, 8000))

        self.view.seek_requested.connect(self.player.seek)
        self.view.edited.connect(self._on_edited)
        self.view.selection_changed.connect(lambda _cid: self._update_state())

        # Atajos que deben funcionar aunque el foco este en la linea de tiempo.
        QShortcut(QKeySequence(Qt.Key_Space), self, activated=self.player.toggle)
        QShortcut(QKeySequence("S"), self, activated=self.split_here)
        QShortcut(QKeySequence("R"), self, activated=lambda: self.rotate_selected(90))
        QShortcut(QKeySequence(Qt.Key_Delete), self, activated=self.delete_selected)
        QShortcut(QKeySequence(Qt.Key_Left), self, activated=lambda: self.player.step(-1))
        QShortcut(QKeySequence(Qt.Key_Right), self, activated=lambda: self.player.step(1))
        QShortcut(QKeySequence("Shift+Left"), self, activated=lambda: self.player.nudge(-2))
        QShortcut(QKeySequence("Shift+Right"), self, activated=lambda: self.player.nudge(2))
        QShortcut(QKeySequence(Qt.Key_Home), self, activated=lambda: self.player.seek(0))
        QShortcut(QKeySequence(Qt.Key_End), self,
                  activated=lambda: self.player.seek(max(0.0, self.timeline.duration - 0.05)))
        QShortcut(QKeySequence("F"), self, activated=self.view.zoom_to_fit)

        self.player.set_volume(self.vol.value())

    def _apply_theme(self) -> None:
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #14161b; color: #dbe1ea; }
            QToolBar { background: #1b1e25; border: 0; padding: 4px; spacing: 2px; }
            QToolBar QToolButton { padding: 6px 10px; border-radius: 4px; }
            QToolBar QToolButton:hover { background: #2a2f3a; }
            QPushButton { background: #262b34; border: 1px solid #333944;
                          border-radius: 4px; padding: 5px 10px; }
            QPushButton:hover { background: #313845; }
            QPushButton:pressed { background: #3d4655; }
            QStatusBar { background: #1b1e25; color: #93a0b4; }
            QLabel { color: #c3ccd9; }
            QComboBox, QLineEdit { background: #21262e; border: 1px solid #333944;
                                   border-radius: 4px; padding: 4px; }
            QScrollBar:horizontal { background: #1b1e25; height: 12px; }
            QScrollBar::handle:horizontal { background: #3c4453; border-radius: 6px;
                                            min-width: 30px; }
            QProgressBar { background: #21262e; border: 1px solid #333944;
                           border-radius: 4px; text-align: center; }
            QProgressBar::chunk { background: #3a7fb0; border-radius: 3px; }
        """)

    # ---------- estado ----------

    def _update_state(self) -> None:
        has = bool(self.timeline.clips)
        sel = self.view.selected_clip()
        self.act_split.setEnabled(has)
        self.act_delete.setEnabled(sel is not None)
        self.act_rot_l.setEnabled(sel is not None)
        self.act_rot_r.setEnabled(sel is not None)
        self.act_rot_all.setEnabled(has)
        self.act_export.setEnabled(has)
        self.act_save_prj.setEnabled(has)
        self.act_undo.setEnabled(self.history.can_undo())
        self.act_redo.setEnabled(self.history.can_redo())
        self._update_time()

        if has:
            n = len(self.timeline.clips)
            total = sum(c.source.size_bytes for c in
                        {c.source.path: c for c in self.timeline.clips}.values())
            extra = f"  ·  seleccionado: {sel.name}" if sel else ""
            self.statusBar().showMessage(
                f"{n} clip{'s' if n != 1 else ''}  ·  "
                f"{timecode(self.timeline.duration)}  ·  "
                f"material: {human_size(total)}{extra}")

    def _update_time(self) -> None:
        fps = self.timeline.fps
        self.lbl_time.setText(
            f"{timecode(self.view.playhead, fps)} / "
            f"{timecode(self.timeline.duration, fps)}")

    def _on_position(self, t: float) -> None:
        self.view.set_playhead(t)
        self.panel._sync_bar()
        self._update_time()

    def _on_volume(self, value: int) -> None:
        self.player.set_volume(value)
        self.settings.setValue("volume", value)

    def _on_edited(self, description: str) -> None:
        """Las ediciones hechas desde la linea de tiempo (arrastre) llegan aqui."""
        self.history.push()
        self._commit(description)

    def _commit(self, message: str) -> None:
        """Refresca previsualizacion, linea de tiempo y estado tras una edicion."""
        self.player.set_timeline(self.timeline)
        self.panel.refresh()
        self._update_state()
        if message:
            self.statusBar().showMessage(message, 3000)

    # ---------- acciones ----------

    def import_video(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Importar video", self.settings.value("last_dir", ""), VIDEO_FILTER)
        if paths:
            self.settings.setValue("last_dir", str(Path(paths[0]).parent))
            self.add_files(paths)

    def add_files(self, paths: list[str]) -> None:
        if not paths:
            return
        self.history.push()
        QApplication.setOverrideCursor(Qt.BusyCursor)
        added, errors = 0, []
        try:
            for p in paths:
                try:
                    self.timeline.append_source(p)
                    added += 1
                except Exception as exc:
                    errors.append(f"{Path(p).name}: {exc}")
        finally:
            QApplication.restoreOverrideCursor()

        if not added:
            self.history.undo()
        self._commit(f"{added} video(s) importado(s)" if added else "")
        if added and len(self.timeline.clips) == added:
            self.view.zoom_to_fit()
            self.player.seek(0)
        if errors:
            self._message(self, QMessageBox.Warning, "No se pudieron importar",
                          NEWLINE.join(errors)).exec()

    def split_here(self) -> None:
        if not self.timeline.clips:
            return
        self.history.push()
        idx = self.timeline.split_at(self.view.playhead)
        if idx is None:
            self.history.undo()
            self.statusBar().showMessage(
                "El cabezal esta sobre el borde de un clip: ahi no hay nada que dividir.",
                4000)
            return
        # Deja seleccionado el trozo de la derecha: lo normal tras cortar es
        # querer borrar o mover justo ese.
        self.view.select(self.timeline.clips[idx].cid)
        self._commit("Clip dividido")

    def delete_selected(self) -> None:
        clip = self.view.selected_clip()
        if clip is None:
            self.statusBar().showMessage(
                "Selecciona antes un clip en la linea de tiempo.", 3000)
            return
        self.history.push()
        pos = self.timeline.start_of(self.timeline.index_of(clip.cid))
        self.timeline.delete(clip.cid)
        self.view.select(-1)
        self._commit("Segmento eliminado")
        self.player.seek(min(pos, max(0.0, self.timeline.duration - 0.05)))

    def rotate_selected(self, delta: int) -> None:
        clip = self.view.selected_clip()
        if clip is None:
            if len(self.timeline.clips) == 1:
                clip = self.timeline.clips[0]
                self.view.select(clip.cid)
            else:
                self.statusBar().showMessage(
                    "Selecciona un clip, o usa 'Girar TODO 90°'.", 3000)
                return
        self.history.push()
        self.timeline.rotate(clip.cid, delta)
        self.player.refresh_rotation()
        self._commit(f"Clip girado a {clip.rotate}°")

    def rotate_all(self) -> None:
        if not self.timeline.clips:
            return
        self.history.push()
        self.timeline.rotate_all(90)
        self.player.refresh_rotation()
        self._commit("Todos los clips girados 90°")

    def undo(self) -> None:
        if self.history.undo():
            self.player.refresh_rotation()
            self._commit("Deshecho")

    def redo(self) -> None:
        if self.history.redo():
            self.player.refresh_rotation()
            self._commit("Rehecho")

    # ---------- proyecto ----------

    def save_project(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Guardar proyecto", self.settings.value("last_dir", ""),
            "Proyecto SW (*.swproj)")
        if not path:
            return
        if not path.lower().endswith(".swproj"):
            path += ".swproj"
        try:
            self.timeline.save(path)
            self.statusBar().showMessage(f"Proyecto guardado en {path}", 5000)
        except Exception as exc:
            self._message(self, QMessageBox.Critical, "Error",
                          f"No se pudo guardar: {exc}").exec()

    def open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Abrir proyecto", self.settings.value("last_dir", ""),
            "Proyecto SW (*.swproj)")
        if not path:
            return
        try:
            missing = self.timeline.load(path)
        except Exception as exc:
            self._message(self, QMessageBox.Critical, "Error",
                          f"No se pudo abrir: {exc}").exec()
            return
        self.history.clear()
        self.view.select(-1)
        self._commit("Proyecto abierto")
        self.view.zoom_to_fit()
        self.player.seek(0)
        if missing:
            self._message(
                self, QMessageBox.Warning, "Archivos no encontrados",
                "Estos videos ya no estan donde se guardaron:" + NEWLINE +
                NEWLINE.join(missing)).exec()

    # ---------- exportacion ----------

    def export(self) -> None:
        if not self.timeline.clips:
            return
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "Exportando", "Ya hay una exportacion en curso.")
            return

        self.player.pause()
        dlg = ExportDialog(self.timeline, self)
        if dlg.exec() != QDialog.Accepted or not dlg.out_path:
            return

        # Exportar sobre un archivo que es fuente del proyecto lo corromperia.
        out = str(Path(dlg.out_path).resolve()).lower()
        if any(c.source.path.lower() == out for c in self.timeline.clips):
            QMessageBox.critical(
                self, "Destino invalido",
                "No puedes exportar encima de un video que estas usando en el "
                "proyecto. Elige otro nombre.")
            return

        self.worker = ExportWorker(
            self.timeline, dlg.out_path,
            mode=str(dlg.mode.currentData()),
            quality=int(dlg.quality.currentData()),
            encoder=str(dlg.encoder.currentData()), parent=self)

        self.progress_dlg = ProgressDialog(self)
        self.progress_dlg.cancel_btn.clicked.connect(self._cancel_export)
        self.worker.progress.connect(self.progress_dlg.update_progress)
        self.worker.finished_ok.connect(self._export_done)
        self.worker.failed.connect(self._export_failed)
        self.worker.start()
        self.progress_dlg.exec()

    def _cancel_export(self) -> None:
        if self.worker:
            self.worker.cancel()
        if self.progress_dlg:
            self.progress_dlg.reject()
        self.statusBar().showMessage("Exportacion cancelada", 4000)

    def _close_progress(self) -> None:
        if self.progress_dlg:
            self.progress_dlg.accept()
            self.progress_dlg = None

    def _export_done(self, path: str, summary: str) -> None:
        self._close_progress()
        size = human_size(Path(path).stat().st_size) if Path(path).exists() else "?"
        box = self._message(
            self, QMessageBox.Information, "Exportacion terminada",
            f"Video guardado:" + NEWLINE + f"{path}" + NEWLINE + NEWLINE +
            f"Tamanio: {size}", summary)
        open_btn = box.addButton("Abrir carpeta", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Ok)
        box.exec()
        if box.clickedButton() is open_btn:
            self._reveal(Path(path))

    def _export_failed(self, message: str) -> None:
        self._close_progress()
        self._message(self, QMessageBox.Critical, "Fallo la exportacion",
                      message).exec()

    # ---------- utilidades ----------

    @staticmethod
    def _message(parent, icon, title: str, text: str,
                 informative: str = "") -> QMessageBox:
        """Cuadro de dialogo que muestra el texto tal cual, sin interpretarlo.

        Por defecto QMessageBox usa Qt::AutoText: si detecta algo que parece
        marcado lo renderiza como HTML. Los mensajes de aqui llevan dentro rutas
        y nombres de archivo elegidos por quien sea, asi que se fija el formato a
        texto plano en vez de dejar que Qt adivine.
        """
        box = QMessageBox(parent)
        box.setIcon(icon)
        box.setWindowTitle(title)
        box.setTextFormat(Qt.PlainText)
        box.setText(text)
        if informative:
            box.setInformativeText(informative)
        return box

    @staticmethod
    def _reveal(target: Path) -> None:
        """Abre el explorador en el archivo exportado.

        Se invoca por ruta absoluta a %WINDIR%: CreateProcess busca primero en el
        directorio de la aplicacion y en el de trabajo, asi que lanzar
        'explorer' por nombre dejaria que un explorer.exe puesto ahi por otro
        gane la resolucion. Sin shell, y la ruta va como argumento aparte.
        """
        windir = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
        if not windir:
            return
        explorer = Path(windir) / "explorer.exe"
        if not explorer.is_file():
            return
        try:
            subprocess.Popen([str(explorer), "/select," + str(target.resolve())])
        except OSError:
            pass

    # ---------- arrastrar y soltar ----------

    def dragEnterEvent(self, event) -> None:  # pragma: no cover - Qt
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # pragma: no cover - Qt
        paths = [u.toLocalFile() for u in event.mimeData().urls()
                 if u.isLocalFile() and Path(u.toLocalFile()).is_file()]
        if paths:
            self.add_files(paths)
            event.acceptProposedAction()

    def closeEvent(self, event) -> None:  # pragma: no cover - Qt
        if self.worker and self.worker.isRunning():
            if QMessageBox.question(
                    self, "Exportacion en curso",
                    "Hay una exportacion sin terminar. ¿Cerrar de todas formas?"
            ) != QMessageBox.Yes:
                event.ignore()
                return
            self.worker.cancel()
            self.worker.wait(3000)
        self.view.thumbs.shutdown()
        self.player.shutdown()
        super().closeEvent(event)
