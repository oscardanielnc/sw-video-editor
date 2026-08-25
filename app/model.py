"""Modelo de datos del proyecto: fuentes, clips y linea de tiempo.

La linea de tiempo es magnetica: los clips siempre van pegados uno detras de
otro, sin huecos. Mover un clip es reordenarlo, no dejarlo flotando.
"""
from __future__ import annotations

import copy
import itertools
import json
from dataclasses import dataclass, field
from pathlib import Path

from .ffmpeg_tools import Source, UnsafePathError, probe, safe_path

MAX_PROJECT_BYTES = 8 * 1024 * 1024   # un .swproj real son unos pocos KB
MAX_CLIPS = 10_000


class ProjectError(ValueError):
    """El archivo de proyecto esta mal formado o pide algo que no se permite."""


def _finite(value, label: str) -> float:
    """Acepta solo numeros reales: JSON admite NaN e Infinity, y ambos envenenan
    todos los calculos de duracion aguas abajo sin dar ningun error."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProjectError(f"{label} deberia ser un numero, no {value!r}.")
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        raise ProjectError(f"{label} no es un numero finito.")
    return value


_ids = itertools.count(1)


@dataclass
class Clip:
    """Un tramo [in_t, out_t) de una fuente, colocado en la linea de tiempo."""

    source: Source
    in_t: float
    out_t: float
    rotate: int = 0            # rotacion extra aplicada por el usuario (0/90/180/270)
    cid: int = field(default_factory=lambda: next(_ids))

    @property
    def duration(self) -> float:
        return max(0.0, self.out_t - self.in_t)

    @property
    def effective_rotation(self) -> int:
        return (self.source.rotation + self.rotate) % 360

    @property
    def name(self) -> str:
        return self.source.name

    def copy_with(self, **kw) -> "Clip":
        data = dict(source=self.source, in_t=self.in_t, out_t=self.out_t,
                    rotate=self.rotate)
        data.update(kw)
        return Clip(**data)


class Timeline:
    """Secuencia ordenada de clips, con posiciones derivadas (nunca almacenadas)."""

    def __init__(self) -> None:
        self.clips: list[Clip] = []
        self.sources: dict[str, Source] = {}

    # ---------- consultas ----------

    @property
    def duration(self) -> float:
        return sum(c.duration for c in self.clips)

    @property
    def fps(self) -> float:
        return self.clips[0].source.fps if self.clips else 30.0

    def starts(self) -> list[float]:
        """Tiempo de inicio de cada clip dentro de la linea de tiempo."""
        out, acc = [], 0.0
        for c in self.clips:
            out.append(acc)
            acc += c.duration
        return out

    def clip_at(self, t: float) -> tuple[int, float] | tuple[None, None]:
        """Indice del clip bajo el tiempo t y el offset dentro de ese clip."""
        acc = 0.0
        for i, c in enumerate(self.clips):
            if t < acc + c.duration or (i == len(self.clips) - 1 and t <= acc + c.duration):
                return i, t - acc
            acc += c.duration
        return None, None

    def index_of(self, cid: int) -> int | None:
        for i, c in enumerate(self.clips):
            if c.cid == cid:
                return i
        return None

    def start_of(self, index: int) -> float:
        return sum(c.duration for c in self.clips[:index])

    # ---------- operaciones ----------

    def add_source(self, path: str) -> Source:
        key = str(safe_path(path))
        if key not in self.sources:
            self.sources[key] = probe(key)
        return self.sources[key]

    def append_source(self, path: str) -> Clip:
        src = self.add_source(path)
        clip = Clip(source=src, in_t=0.0, out_t=src.duration)
        self.clips.append(clip)
        return clip

    def split_at(self, t: float) -> int | None:
        """Divide el clip que esta bajo t. Devuelve el indice del segundo trozo."""
        idx, offset = self.clip_at(t)
        if idx is None:
            return None
        clip = self.clips[idx]
        cut = clip.in_t + offset
        # No dividir si el corte cae practicamente en un borde: no crea nada util.
        min_len = max(clip.source.frame_dur, 0.001)
        if cut - clip.in_t < min_len or clip.out_t - cut < min_len:
            return None
        left = clip.copy_with(out_t=cut)
        right = clip.copy_with(in_t=cut)
        self.clips[idx:idx + 1] = [left, right]
        return idx + 1

    def delete(self, cid: int) -> bool:
        idx = self.index_of(cid)
        if idx is None:
            return False
        del self.clips[idx]
        return True

    def move(self, cid: int, new_index: int) -> bool:
        """Reordena un clip. new_index es la posicion final deseada en la lista."""
        idx = self.index_of(cid)
        if idx is None:
            return False
        new_index = max(0, min(len(self.clips) - 1, new_index))
        if new_index == idx:
            return False
        clip = self.clips.pop(idx)
        self.clips.insert(new_index, clip)
        return True

    def rotate(self, cid: int, delta: int = 90) -> bool:
        idx = self.index_of(cid)
        if idx is None:
            return False
        self.clips[idx].rotate = (self.clips[idx].rotate + delta) % 360
        return True

    def rotate_all(self, delta: int = 90) -> None:
        for c in self.clips:
            c.rotate = (c.rotate + delta) % 360

    # ---------- persistencia ----------

    def snapshot(self) -> list[Clip]:
        """Copia superficial para el historial de deshacer.

        Los Source son inmutables y se comparten a proposito: copiarlos seria
        desperdiciar memoria sin ganar nada.
        """
        return [copy.copy(c) for c in self.clips]

    def restore(self, snap: list[Clip]) -> None:
        self.clips = [copy.copy(c) for c in snap]

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "clips": [
                {"path": c.source.path, "in": c.in_t, "out": c.out_t,
                 "rotate": c.rotate}
                for c in self.clips
            ],
        }

    def save(self, path: str) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    def load(self, path: str) -> list[str]:
        """Carga un proyecto. Devuelve la lista de archivos que ya no existen.

        Un .swproj es JSON y puede venir de otra persona, asi que se trata como
        entrada no confiable: se valida el tamanio antes de parsear, cada campo
        por tipo y rango, y cada ruta pasa por safe_path para que no pueda
        apuntar a algo que no sea un archivo local existente.
        """
        prj = Path(path)
        size = prj.stat().st_size
        if size > MAX_PROJECT_BYTES:
            raise ProjectError(
                f"El proyecto ocupa {size} bytes; el limite son "
                f"{MAX_PROJECT_BYTES}.")

        try:
            data = json.loads(prj.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProjectError(f"El archivo no es un proyecto valido: {exc}") from exc
        if not isinstance(data, dict):
            raise ProjectError("El archivo no es un proyecto valido.")
        if data.get("version") != 1:
            raise ProjectError(
                f"Version de proyecto no soportada: {data.get('version')!r}.")

        items = data.get("clips")
        if not isinstance(items, list):
            raise ProjectError("El proyecto no contiene una lista de clips.")
        if len(items) > MAX_CLIPS:
            raise ProjectError(
                f"El proyecto declara {len(items)} clips; el limite son {MAX_CLIPS}.")

        self.clips = []
        missing: list[str] = []
        for n, item in enumerate(items, 1):
            if not isinstance(item, dict):
                raise ProjectError(f"El clip {n} esta mal formado.")
            raw_path = item.get("path")
            if not isinstance(raw_path, str):
                raise ProjectError(f"El clip {n} no indica ninguna ruta.")
            in_t = _finite(item.get("in"), f"clip {n}: 'in'")
            out_t = _finite(item.get("out"), f"clip {n}: 'out'")
            rotate = item.get("rotate", 0)
            if rotate not in (0, 90, 180, 270):
                raise ProjectError(f"El clip {n} tiene una rotacion invalida: {rotate!r}.")
            if in_t < 0 or out_t <= in_t:
                raise ProjectError(f"El clip {n} tiene un intervalo invalido.")

            try:
                src = self.add_source(raw_path)
            except UnsafePathError:
                # Un video que ya no esta donde se guardo es lo normal; se avisa
                # y se sigue. Cualquier otro fallo de probe si es un error real.
                missing.append(raw_path)
                continue
            # El recorte guardado no puede pasarse del final real del archivo:
            # el video pudo cambiar desde que se guardo el proyecto.
            self.clips.append(Clip(source=src, in_t=min(in_t, src.duration),
                                   out_t=min(out_t, src.duration), rotate=rotate))
        return missing


class History:
    """Pila de deshacer/rehacer sobre instantaneas de la linea de tiempo."""

    def __init__(self, timeline: Timeline, limit: int = 100) -> None:
        self.tl = timeline
        self.limit = limit
        self._undo: list[list[Clip]] = []
        self._redo: list[list[Clip]] = []

    def push(self) -> None:
        """Guarda el estado ACTUAL. Llamar justo antes de modificar."""
        self._undo.append(self.tl.snapshot())
        if len(self._undo) > self.limit:
            self._undo.pop(0)
        self._redo.clear()

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self.tl.snapshot())
        self.tl.restore(self._undo.pop())
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self.tl.snapshot())
        self.tl.restore(self._redo.pop())
        return True

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
