"""Linea de tiempo dibujada a mano: regla, clips con filmstrip, cabezal y arrastre.

Se pinta con QPainter en vez de usar QGraphicsView porque el contenido es una
sola fila de rectangulos: el control directo del repintado evita trabajo inutil
cuando el cabezal se mueve 60 veces por segundo.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QPainter, QPen,
                           QPixmap, QPolygon)
from PySide6.QtWidgets import (QHBoxLayout, QScrollBar, QSizePolicy, QVBoxLayout,
                               QWidget)

from .ffmpeg_tools import timecode
from .model import Timeline
from .thumbs import ThumbProvider

RULER_H = 26
TRACK_TOP = RULER_H + 8
TRACK_H = 104
AUDIO_H = 20
BOTTOM_PAD = 12
WIDGET_H = TRACK_TOP + TRACK_H + BOTTOM_PAD

C_BG = QColor("#16181d")
C_RULER = QColor("#1e2128")
C_TICK = QColor("#5a6070")
C_TEXT = QColor("#c8cdd8")
C_CLIP = QColor("#2f6f9f")
C_CLIP_ALT = QColor("#3a7fb0")
C_CLIP_SEL = QColor("#f0a030")
C_AUDIO = QColor("#1d4a30")
C_PLAYHEAD = QColor("#ff4757")
C_INSERT = QColor("#ffd166")


class TimelineView(QWidget):
    """Dibujo e interaccion. El scroll horizontal lo gestiona TimelinePanel."""

    seek_requested = Signal(float)
    selection_changed = Signal(int)      # cid del clip, o -1
    edited = Signal(str)                 # descripcion de la operacion, para el historial
    zoom_changed = Signal()

    DRAG_THRESHOLD = 6

    def __init__(self, timeline: Timeline, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tl = timeline
        self.pps = 40.0            # pixeles por segundo
        self.offset = 0.0          # segundos en el borde izquierdo
        self.playhead = 0.0
        self.selected_cid = -1

        self.thumbs = ThumbProvider(self)
        self.thumbs.updated.connect(self.update)

        self._press_pos: QPoint | None = None
        self._press_cid = -1
        self._dragging = False
        self._drag_mouse_x = 0
        self._drag_grab_dt = 0.0    # segundos entre el inicio del clip y el cursor
        self._insert_index = -1
        self._scrubbing = False

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumHeight(WIDGET_H)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.ArrowCursor)

    # ---------- conversiones ----------

    def t2x(self, t: float) -> float:
        return (t - self.offset) * self.pps

    def x2t(self, x: float) -> float:
        return self.offset + x / self.pps

    def content_seconds(self) -> float:
        return max(self.tl.duration, 1.0)

    def visible_seconds(self) -> float:
        return max(0.1, self.width() / self.pps)

    # ---------- estado ----------

    def set_playhead(self, t: float, follow: bool = True) -> None:
        if abs(t - self.playhead) < 1e-4:
            return
        self.playhead = t
        if follow:
            self._ensure_visible(t)
        self.update()

    def _ensure_visible(self, t: float) -> None:
        vis = self.visible_seconds()
        if t < self.offset + vis * 0.05:
            self.offset = max(0.0, t - vis * 0.25)
            self.zoom_changed.emit()
        elif t > self.offset + vis * 0.92:
            self.offset = max(0.0, t - vis * 0.7)
            self.zoom_changed.emit()

    def set_offset(self, seconds: float) -> None:
        self.offset = max(0.0, seconds)
        self.update()

    def set_zoom(self, pps: float, anchor_t: float | None = None) -> None:
        pps = max(0.05, min(600.0, pps))
        if abs(pps - self.pps) < 1e-6:
            return
        if anchor_t is None:
            anchor_t = self.playhead
        anchor_x = self.t2x(anchor_t)
        self.pps = pps
        self.offset = max(0.0, anchor_t - anchor_x / self.pps)
        self.zoom_changed.emit()
        self.update()

    def zoom_to_fit(self) -> None:
        if self.tl.duration <= 0 or self.width() <= 20:
            return
        self.offset = 0.0
        self.pps = max(0.05, (self.width() - 20) / self.tl.duration)
        self.zoom_changed.emit()
        self.update()

    def select(self, cid: int) -> None:
        if cid != self.selected_cid:
            self.selected_cid = cid
            self.selection_changed.emit(cid)
            self.update()

    def selected_clip(self):
        idx = self.tl.index_of(self.selected_cid)
        return self.tl.clips[idx] if idx is not None else None

    def refresh(self) -> None:
        if self.tl.index_of(self.selected_cid) is None:
            self.select(-1)
        self.update()

    # ---------- geometria de clips ----------

    def clip_rects(self, skip_cid: int = -1) -> list[tuple]:
        """[(clip, indice, QRectF)] de los clips que tocan la zona visible."""
        out = []
        acc = 0.0
        left_t, right_t = self.offset, self.offset + self.visible_seconds()
        for i, clip in enumerate(self.tl.clips):
            start, end = acc, acc + clip.duration
            acc = end
            if clip.cid == skip_cid or end < left_t or start > right_t:
                continue
            x0 = self.t2x(start)
            x1 = self.t2x(end)
            out.append((clip, i, QRectF(x0, TRACK_TOP, max(1.0, x1 - x0), TRACK_H)))
        return out

    def clip_at_pos(self, pos: QPoint):
        if pos.y() < TRACK_TOP or pos.y() > TRACK_TOP + TRACK_H:
            return None, None
        for clip, i, rect in self.clip_rects():
            if rect.left() <= pos.x() <= rect.right():
                return clip, i
        return None, None

    # ---------- pintado ----------

    def paintEvent(self, event) -> None:  # pragma: no cover - Qt
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        p.fillRect(self.rect(), C_BG)

        self._paint_ruler(p)

        if not self.tl.clips:
            p.setPen(QPen(QColor("#5a6070")))
            f = QFont(); f.setPointSize(10); p.setFont(f)
            p.drawText(self.rect().adjusted(0, TRACK_TOP, 0, 0), Qt.AlignHCenter | Qt.AlignTop,
                       "Arrastra un video aqui  ·  o pulsa Ctrl+O para importar")
            p.end()
            return

        skip = self.selected_cid if self._dragging else -1
        for clip, idx, rect in self.clip_rects(skip_cid=skip):
            self._paint_clip(p, clip, idx, rect, selected=(clip.cid == self.selected_cid))

        if self._dragging:
            self._paint_drag(p)

        self._paint_playhead(p)
        p.end()

    def _paint_ruler(self, p: QPainter) -> None:
        p.fillRect(QRect(0, 0, self.width(), RULER_H), C_RULER)
        p.setPen(QPen(QColor("#2a2e37")))
        p.drawLine(0, RULER_H - 1, self.width(), RULER_H - 1)

        step = self._tick_step()
        f = QFont("Consolas"); f.setPointSize(8); p.setFont(f)
        fm = QFontMetrics(f)

        first = int(self.offset / step)
        t = first * step
        end_t = self.offset + self.visible_seconds()
        while t <= end_t:
            x = self.t2x(t)
            if x >= -50:
                p.setPen(QPen(C_TICK))
                p.drawLine(int(x), RULER_H - 8, int(x), RULER_H - 1)
                label = timecode(t)[:8] if step < 60 else timecode(t)[:5]
                p.setPen(QPen(C_TEXT))
                p.drawText(int(x) + 3, RULER_H - 10 + fm.ascent() // 2, label)
            t += step

    def _tick_step(self) -> float:
        """Elige un intervalo de marcas que quede legible al zoom actual."""
        target_px = 90.0
        raw = target_px / self.pps
        for step in (0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300,
                     600, 900, 1800, 3600):
            if step >= raw:
                return float(step)
        return 7200.0

    def _paint_clip(self, p: QPainter, clip, idx: int, rect: QRectF,
                    selected: bool) -> None:
        body = QRectF(rect.left(), rect.top(), rect.width(), rect.height() - AUDIO_H)
        base = C_CLIP if idx % 2 == 0 else C_CLIP_ALT

        p.setClipRect(body)
        p.fillRect(body, base.darker(160))
        self._paint_filmstrip(p, clip, body)
        p.setClipping(False)

        # Pista de audio pegada al clip: se corta y se mueve con el.
        if clip.source.has_audio:
            arect = QRectF(rect.left(), body.bottom(), rect.width(), AUDIO_H)
            p.fillRect(arect, C_AUDIO)
            p.setPen(QPen(QColor("#3f8f5f")))
            mid = arect.center().y()
            p.drawLine(int(arect.left()) + 2, int(mid), int(arect.right()) - 2, int(mid))

        # Etiqueta con nombre, duracion y rotacion.
        if rect.width() > 46:
            p.setClipRect(rect)
            label = QRectF(rect.left(), rect.top(), rect.width(), 17)
            p.fillRect(label, QColor(0, 0, 0, 150))
            f = QFont(); f.setPointSize(8); p.setFont(f)
            p.setPen(QPen(QColor("#e8ecf4")))
            text = f" {clip.name}  ·  {timecode(clip.duration)[:8]}"
            if clip.rotate:
                text += f"  ·  {clip.rotate}°"
            p.drawText(label.adjusted(2, 0, -2, 0), Qt.AlignVCenter | Qt.AlignLeft, text)
            p.setClipping(False)

        pen = QPen(C_CLIP_SEL if selected else QColor("#101216"), 3 if selected else 1)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))

    def _paint_filmstrip(self, p: QPainter, clip, body: QRectF) -> None:
        src = clip.source
        dw, dh = src.display_size(clip.rotate)
        if dh <= 0:
            return
        th = int(body.height())
        tw = max(24, int(th * (dw / dh)))

        # Solo la parte visible: en un clip de 3 horas eso son unas pocas miniaturas.
        x = body.left()
        vis_left, vis_right = 0.0, float(self.width())
        # Alinear a la rejilla para que las miniaturas no bailen al hacer scroll.
        n_skip = max(0, int((vis_left - body.left()) // tw))
        x += n_skip * tw
        guard = 0
        while x < body.right() and x < vis_right and guard < 400:
            guard += 1
            src_t = clip.in_t + (x - body.left()) / self.pps
            if src_t > clip.out_t:
                break
            pix = self.thumbs.get(src.path, min(src_t, src.duration - 0.05), th)
            slot = QRectF(x, body.top(), min(tw, body.right() - x), body.height())
            if pix is not None and not pix.isNull():
                p.drawPixmap(slot, pix, QRectF(0, 0, min(pix.width(), slot.width()),
                                               pix.height()))
            x += tw

    def _paint_drag(self, p: QPainter) -> None:
        clip = self.selected_clip()
        if clip is None:
            return
        w = clip.duration * self.pps
        x = self._drag_mouse_x - self._drag_grab_dt * self.pps
        ghost = QRectF(x, TRACK_TOP - 4, max(2.0, w), TRACK_H + 8)
        p.setOpacity(0.75)
        p.fillRect(ghost, C_CLIP_SEL.darker(200))
        p.setOpacity(1.0)
        p.setPen(QPen(C_CLIP_SEL, 2))
        p.setBrush(Qt.NoBrush)
        p.drawRect(ghost)

        if self._insert_index >= 0:
            others = [c for c in self.tl.clips if c.cid != clip.cid]
            mark_t = sum(c.duration for c in others[:self._insert_index])
            mx = self.t2x(mark_t)
            p.setPen(QPen(C_INSERT, 3))
            p.drawLine(int(mx), TRACK_TOP - 6, int(mx), TRACK_TOP + TRACK_H + 6)

    def _paint_playhead(self, p: QPainter) -> None:
        x = self.t2x(self.playhead)
        if -10 <= x <= self.width() + 10:
            p.setPen(QPen(C_PLAYHEAD, 2))
            p.drawLine(int(x), 0, int(x), TRACK_TOP + TRACK_H + 4)
            p.setBrush(QBrush(C_PLAYHEAD))
            p.setPen(Qt.NoPen)
            p.drawPolygon(QPolygon([QPoint(int(x) - 6, 0), QPoint(int(x) + 6, 0),
                                    QPoint(int(x), 9)]))

    # ---------- raton ----------

    def mousePressEvent(self, event) -> None:  # pragma: no cover - Qt
        if event.button() != Qt.LeftButton:
            return
        pos = event.position().toPoint()
        self._press_pos = pos
        self._dragging = False

        if pos.y() < TRACK_TOP:
            self._scrubbing = True
            self.seek_requested.emit(max(0.0, self.x2t(pos.x())))
            return

        clip, _idx = self.clip_at_pos(pos)
        if clip is None:
            self._press_cid = -1
            self.select(-1)
            self.seek_requested.emit(max(0.0, min(self.x2t(pos.x()), self.tl.duration)))
            return

        self._press_cid = clip.cid
        self.select(clip.cid)
        start = self.tl.start_of(self.tl.index_of(clip.cid))
        self._drag_grab_dt = self.x2t(pos.x()) - start
        self.seek_requested.emit(max(0.0, min(self.x2t(pos.x()), self.tl.duration)))

    def mouseMoveEvent(self, event) -> None:  # pragma: no cover - Qt
        pos = event.position().toPoint()

        if self._scrubbing:
            self.seek_requested.emit(max(0.0, min(self.x2t(pos.x()), self.tl.duration)))
            return

        if self._press_pos is None or self._press_cid < 0:
            over, _ = self.clip_at_pos(pos)
            self.setCursor(Qt.OpenHandCursor if over else Qt.ArrowCursor)
            return

        if not self._dragging:
            if abs(pos.x() - self._press_pos.x()) < self.DRAG_THRESHOLD:
                return
            self._dragging = True
            self.setCursor(Qt.ClosedHandCursor)

        self._drag_mouse_x = pos.x()
        self._insert_index = self._insertion_index(pos.x())
        self._auto_scroll(pos.x())
        self.update()

    def _insertion_index(self, mouse_x: float) -> int:
        """Posicion donde caeria el clip arrastrado, ignorandolo a el mismo."""
        others = [c for c in self.tl.clips if c.cid != self._press_cid]
        drop_t = self.x2t(mouse_x - self._drag_grab_dt * self.pps)
        acc, index = 0.0, 0
        for c in others:
            if drop_t > acc + c.duration / 2:
                index += 1
            acc += c.duration
        return index

    def _auto_scroll(self, x: float) -> None:
        margin = 40
        if x < margin:
            self.offset = max(0.0, self.offset - 12 / self.pps)
            self.zoom_changed.emit()
        elif x > self.width() - margin:
            self.offset += 12 / self.pps
            self.zoom_changed.emit()

    def mouseReleaseEvent(self, event) -> None:  # pragma: no cover - Qt
        if self._dragging and self._press_cid >= 0 and self._insert_index >= 0:
            # _insert_index se calculo sobre la lista sin el clip arrastrado, que es
            # exactamente el estado en el que move() hace el insert tras el pop.
            if self.tl.move(self._press_cid, self._insert_index):
                self.edited.emit("Mover clip")

        self._dragging = False
        self._press_pos = None
        self._press_cid = -1
        self._insert_index = -1
        self._scrubbing = False
        self.setCursor(Qt.ArrowCursor)
        self.update()

    def wheelEvent(self, event) -> None:  # pragma: no cover - Qt
        delta = event.angleDelta().y()
        if event.modifiers() & Qt.ControlModifier:
            anchor = self.x2t(event.position().x())
            self.set_zoom(self.pps * (1.25 if delta > 0 else 0.8), anchor_t=anchor)
        else:
            self.set_offset(self.offset - (delta / 120.0) * (60 / self.pps) * 2)
            self.zoom_changed.emit()
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # pragma: no cover - Qt
        clip, _ = self.clip_at_pos(event.position().toPoint())
        if clip is not None:
            self.seek_requested.emit(self.tl.start_of(self.tl.index_of(clip.cid)))


class TimelinePanel(QWidget):
    """La vista mas su barra de desplazamiento horizontal."""

    def __init__(self, timeline: Timeline, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.view = TimelineView(timeline, self)
        self.bar = QScrollBar(Qt.Horizontal, self)
        self.bar.setSingleStep(10)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.view)
        lay.addWidget(self.bar)

        self.view.zoom_changed.connect(self._sync_bar)
        self.bar.valueChanged.connect(self._on_bar)
        self._syncing = False

    def _sync_bar(self) -> None:
        if self._syncing:
            return
        self._syncing = True
        total = self.view.content_seconds()
        vis = self.view.visible_seconds()
        # Escala en centesimas de segundo: da precision suave sin desbordar el int.
        self.bar.setRange(0, max(0, int((total - vis) * 100)))
        self.bar.setPageStep(max(1, int(vis * 100)))
        self.bar.setValue(int(self.view.offset * 100))
        self._syncing = False

    def _on_bar(self, value: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        self.view.set_offset(value / 100.0)
        self._syncing = False

    def resizeEvent(self, event) -> None:  # pragma: no cover - Qt
        super().resizeEvent(event)
        self._sync_bar()

    def refresh(self) -> None:
        self.view.refresh()
        self._sync_bar()
