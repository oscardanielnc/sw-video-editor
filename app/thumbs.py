"""Generacion perezosa de miniaturas para el filmstrip de la linea de tiempo.

Solo se piden fotogramas del tramo visible, y cada extraccion usa seek rapido por
keyframe, asi que el coste no depende del tamanio del archivo.
"""
from __future__ import annotations

import hashlib
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QPixmap

from .ffmpeg_tools import CACHE, extract_thumb

THUMB_DIR = CACHE / "thumbs"
BUCKET = 0.5          # segundos: redondear permite reutilizar cache entre zooms
MEM_LIMIT = 900       # miniaturas en memoria antes de descartar las mas viejas


def _key(path: str, t: float, height: int) -> str:
    """Nombre del archivo de cache para un fotograma concreto.

    La clave es tambien el nombre del archivo en disco, asi que una colision
    serviria la miniatura de otro video. sha256 truncado a 32 hex deja eso fuera
    del terreno de lo posible; sha1 ya no da esa garantia.
    """
    bucket = round(t / BUCKET) * BUCKET
    raw = f"{path}|{bucket:.2f}|{height}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


class _Signals(QObject):
    done = Signal(str)      # clave de la miniatura terminada


class _Job(QRunnable):
    def __init__(self, provider: "ThumbProvider", key: str, path: str,
                 t: float, height: int) -> None:
        super().__init__()
        self.provider = provider
        self.key = key
        self.path = path
        self.t = t
        self.height = height
        self.setAutoDelete(True)

    def run(self) -> None:  # pragma: no cover - hilo de trabajo
        out = THUMB_DIR / f"{self.key}.jpg"
        ok = out.exists() and out.stat().st_size > 0
        if not ok and not self.provider.closing:
            try:
                ok = extract_thumb(self.path, self.t, self.height, out)
            except Exception:
                ok = False
        self.provider.pending.discard(self.key)
        if not ok:
            self.provider.failed.add(self.key)
            return
        try:
            self.provider.signals.done.emit(self.key)
        except RuntimeError:
            # La ventana se cerro mientras esta miniatura se generaba: el objeto
            # de senales ya no existe y no hay nada a lo que avisar.
            pass


class ThumbProvider(QObject):
    """Cache de miniaturas en memoria + disco, alimentada por un pool de hilos."""

    updated = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        self._mem: OrderedDict[str, QPixmap] = OrderedDict()
        self.pending: set[str] = set()
        self.failed: set[str] = set()
        self.closing = False
        self.signals = _Signals()
        self.signals.done.connect(self._on_done)

        self.pool = QThreadPool(self)
        # Tres ffmpeg a la vez: suficiente para llenar la vista sin ahogar el disco.
        self.pool.setMaxThreadCount(3)

    def shutdown(self) -> None:
        """Descarta lo pendiente y espera a los ffmpeg en curso antes de cerrar."""
        self.closing = True
        self.pool.clear()
        self.pool.waitForDone(4000)

    def get(self, path: str, t: float, height: int) -> QPixmap | None:
        """Devuelve la miniatura si ya esta lista; si no, la encola y devuelve None."""
        if self.closing:
            return None
        key = _key(path, t, height)
        pix = self._mem.get(key)
        if pix is not None:
            self._mem.move_to_end(key)
            return pix

        if key in self.pending or key in self.failed:
            return None

        disk = THUMB_DIR / f"{key}.jpg"
        if disk.exists() and disk.stat().st_size > 0:
            pix = QPixmap(str(disk))
            if not pix.isNull():
                self._store(key, pix)
                return pix

        self.pending.add(key)
        self.pool.start(_Job(self, key, path, round(t / BUCKET) * BUCKET, height))
        return None

    def _store(self, key: str, pix: QPixmap) -> None:
        self._mem[key] = pix
        self._mem.move_to_end(key)
        while len(self._mem) > MEM_LIMIT:
            self._mem.popitem(last=False)

    def _on_done(self, key: str) -> None:
        disk = THUMB_DIR / f"{key}.jpg"
        if disk.exists():
            pix = QPixmap(str(disk))
            if not pix.isNull():
                self._store(key, pix)
                self.updated.emit()

    def busy(self) -> int:
        return len(self.pending)

    def clear_failures(self) -> None:
        self.failed.clear()
