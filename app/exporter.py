"""Exportacion en dos modos, cada uno correcto por separado.

Historia de por que esto no es un "smart render" clasico. La primera version
partia cada clip en tres: recodificar el fragmento inicial hasta el keyframe,
copiar el centro bit a bit, recodificar el final. Sobre el papel da lo mejor de
los dos mundos. En la practica produce video corrupto, y esta medido:

  - Un flujo H.264 lleva sus parametros de codificacion en las cabeceras SPS/PPS.
  - MP4, MKV y MOV guardan UN solo juego de esas cabeceras por pista.
  - El fragmento recodificado (NVENC) y el tramo copiado (la camara) nunca tienen
    las mismas. Al unirlos por copia, todo lo que no sea el primer trozo se
    decodifica con parametros ajenos.
  - Resultado: 'reference count overflow', la imagen se queda congelada tras cada
    corte hasta el siguiente keyframe, y el audio sigue normal.

Se probaron MPEG-TS, MKV, MP4 y el filtro h264_mp4toannexb: los seis caminos dan
entre 913 y 2761 errores de decodificacion (tests/diag_mix.py). Los trozos en TS
llegan ademas sin ninguna cabecera SPS/PPS (tests/diag_nal.py). No es un ajuste
mal puesto: es un limite del formato.

Asi que se exporta de una de estas dos formas, nunca mezclando:

  SIN RECODIFICAR  Los cortes se llevan al keyframe mas cercano y se copia todo
                   bit a bit. Calidad original exacta y velocidad de disco. A
                   cambio, cada corte se puede desplazar hasta medio GOP; el
                   programa dice cuanto se mueve cada uno antes de exportar.

  EXACTO AL FOTOGRAMA  Se recodifica todo con NVENC en calidad casi sin perdida.
                   El corte cae donde lo pusiste. Todos los trozos salen del
                   mismo codificador, asi que comparten cabeceras y se unen bien.

Los trozos intermedios van en Matroska, que guarda la extradata por pista y deja
cada trozo decodificable por si mismo.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from .ffmpeg_tools import (FFMPEG, FFmpegError, as_input, keyframe_bracket,
                           popen, probe, run)
from .model import Clip, Timeline

COPY = "copy"
ENCODE = "encode"

MODE_LOSSLESS = "lossless"   # copia bit a bit, cortes llevados al keyframe
MODE_EXACT = "exact"         # recodifica todo, el corte cae exacto al fotograma

# Contenedor de los trozos intermedios. Matroska guarda la extradata por pista,
# asi que cada trozo se decodifica solo. Con MPEG-TS los trozos que no empiezan
# en el segundo 0 salian sin cabeceras SPS/PPS y no decodificaban nada.
PIECE_FORMAT = "matroska"
PIECE_EXT = ".mkv"

LF = chr(10)
_QUOTE = "'"
_ESCAPED_QUOTE = "'" + chr(92) + "''"


def _concat_quote(value: str) -> str:
    """Cita una ruta para la lista del demuxer concat de ffmpeg.

    Dentro de comillas simples ffmpeg no interpreta nada, asi que basta con
    cerrar el literal, escapar la comilla y volver a abrirlo, igual que en un
    shell POSIX.
    """
    return _QUOTE + value.replace(_QUOTE, _ESCAPED_QUOTE) + _QUOTE



@dataclass
class Piece:
    clip: Clip
    start: float          # tiempo en la fuente
    end: float
    mode: str             # COPY o ENCODE
    delta_rot: int = 0    # rotacion a grabar en pixeles (solo modo ENCODE)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def frames(self) -> int:
        """Fotogramas exactos del fragmento.

        Se le pasa este numero a ffmpeg con -frames:v en vez de una duracion:
        cortar por tiempo deja fuera o de mas el fotograma del limite segun como
        redondee, y ese error de uno se acumula corte tras corte.
        """
        fps = self.clip.source.fps or 30.0
        return max(1, int(round(self.duration * fps)))

    @property
    def out_duration(self) -> float:
        """Duracion que este trozo ocupara realmente en el archivo final."""
        return self.frames / (self.clip.source.fps or 30.0)


@dataclass
class CutShift:
    """Cuanto se movio cada extremo de un clip al ajustarlo a keyframes."""

    clip_name: str
    in_shift: float
    out_shift: float


@dataclass
class Plan:
    mode: str = MODE_LOSSLESS
    encoder_label: str = "NVENC"
    pieces: list[Piece] = field(default_factory=list)
    shifts: list[CutShift] = field(default_factory=list)
    out_rotation: int = 0
    target_w: int = 0
    target_h: int = 0
    fps: float = 30.0
    copy_audio: bool = False
    any_audio: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def total_duration(self) -> float:
        return sum(p.out_duration for p in self.pieces)

    def clip_durations(self) -> list[tuple[Clip, float]]:
        """Duracion final de cada clip, para que el audio case exactamente con el video."""
        out: list[tuple[Clip, float]] = []
        for piece in self.pieces:
            if out and out[-1][0] is piece.clip:
                out[-1] = (piece.clip, out[-1][1] + piece.out_duration)
            else:
                out.append((piece.clip, piece.out_duration))
        return out

    @property
    def encoded_duration(self) -> float:
        return sum(p.duration for p in self.pieces if p.mode == ENCODE)

    @property
    def lossless(self) -> bool:
        return self.mode == MODE_LOSSLESS

    @property
    def max_shift(self) -> float:
        return max((max(abs(s.in_shift), abs(s.out_shift)) for s in self.shifts),
                   default=0.0)

    def summary(self) -> str:
        if not self.pieces:
            return "Linea de tiempo vacia."
        if self.mode == MODE_LOSSLESS:
            if self.max_shift <= 0.001:
                return ("Copia bit a bit: los cortes ya caian sobre keyframes, "
                        "asi que el video sale identico al original y exacto al "
                        "fotograma.")
            return (f"Copia bit a bit: el video sale identico al original. "
                    f"Los cortes se han llevado al keyframe mas cercano; el que "
                    f"mas se mueve lo hace {self.max_shift:.2f} s.")
        return (f"Recodificado con {self.encoder_label}: los cortes caen exactos "
                f"al fotograma. Calidad muy alta, pero no es una copia del "
                f"original ({self.total_duration:.1f} s de video).")

    def shift_detail(self) -> list[str]:
        out = []
        for s in self.shifts:
            if abs(s.in_shift) > 0.001 or abs(s.out_shift) > 0.001:
                out.append(f"{s.clip_name}: inicio {s.in_shift:+.2f} s, "
                           f"final {s.out_shift:+.2f} s")
        return out


def analyze(timeline: Timeline) -> tuple[bool, str, float]:
    """Comprueba si se puede exportar sin recodificar y cuanto moveria los cortes.

    Devuelve (se_puede_copiar, motivo_si_no, desplazamiento_maximo).
    """
    clips = [c for c in timeline.clips if c.duration > 0]
    if not clips:
        return False, "La linea de tiempo esta vacia.", 0.0

    ref = clips[0]
    ref_sig = ref.source.signature()
    ref_rot = ref.effective_rotation

    for clip in clips:
        if clip.source.signature() != ref_sig:
            return (False, f"'{clip.name}' no coincide en codec, resolucion o fps "
                    f"con '{ref.name}'.", 0.0)
        if clip.effective_rotation != ref_rot:
            return (False, f"'{clip.name}' esta girado de forma distinta a "
                    f"'{ref.name}'.", 0.0)

    plan = build_plan(timeline, MODE_LOSSLESS)
    return True, "", plan.max_shift


def build_plan(timeline: Timeline, mode: str = MODE_LOSSLESS,
               encoder_label: str = "NVENC") -> Plan:
    """Construye el plan de exportacion para el modo pedido."""
    plan = Plan(mode=mode, encoder_label=encoder_label)
    clips = [c for c in timeline.clips if c.duration > 0]
    if not clips:
        return plan

    ref = clips[0]
    plan.out_rotation = ref.effective_rotation
    plan.target_w, plan.target_h = ref.source.width, ref.source.height
    plan.fps = ref.source.fps

    plan.any_audio = any(c.source.has_audio for c in clips)
    if plan.any_audio and not all(c.source.has_audio for c in clips):
        plan.warnings.append(
            "Algunos clips no tienen audio: se les anadira silencio para que la "
            "pista quede continua.")

    if mode == MODE_LOSSLESS:
        ok, reason, _ = _uniform(clips)
        if not ok:
            plan.warnings.append(
                reason + " No se puede copiar sin recodificar, se recodificara todo.")
            plan.mode = MODE_EXACT
        else:
            for clip in clips:
                piece, shift = _snap_to_keyframes(clip)
                plan.pieces.append(piece)
                plan.shifts.append(shift)
            # Copiar el audio solo tiene sentido cuando el video tampoco se toca.
            plan.copy_audio = plan.any_audio
            plan.warnings = list(dict.fromkeys(plan.warnings))
            return plan

    # Modo exacto: todo recodificado, y todos los trozos con los mismos ajustes,
    # que es lo que permite unirlos despues sin corromper nada.
    for clip in clips:
        delta = (clip.effective_rotation - plan.out_rotation) % 360
        plan.pieces.append(Piece(clip, clip.in_t, clip.out_t, ENCODE, delta))
    plan.copy_audio = False
    plan.warnings = list(dict.fromkeys(plan.warnings))
    return plan


def _uniform(clips: list[Clip]) -> tuple[bool, str, None]:
    """Los clips deben compartir flujo y rotacion para poder copiarse."""
    ref = clips[0]
    ref_sig = ref.source.signature()
    ref_rot = ref.effective_rotation
    for clip in clips:
        if clip.source.signature() != ref_sig:
            return False, (f"'{clip.name}' no coincide en codec, resolucion o fps "
                           f"con '{ref.name}'."), None
        if clip.effective_rotation != ref_rot:
            return False, (f"'{clip.name}' esta girado de forma distinta a "
                           f"'{ref.name}'."), None
    return True, "", None


def _snap_to_keyframes(clip: Clip) -> tuple[Piece, "CutShift"]:
    """Lleva los extremos del clip al keyframe mas cercano para poder copiarlo.

    La copia de un flujo H.264 solo puede empezar en un keyframe, y debe terminar
    justo antes de otro para no dejar un GOP a medias con referencias rotas.
    """
    src = clip.source
    tol = 0.5 / src.fps if src.fps > 0 else 0.016

    start = _nearest_keyframe(src.path, clip.in_t, src.fps, tol)
    # El final tambien va a un keyframe, salvo que el clip llegue hasta el final
    # del archivo: ahi se copia hasta el ultimo fotograma.
    if clip.out_t >= src.duration - tol:
        end = src.duration
        out_shift = 0.0
    else:
        end = _nearest_keyframe(src.path, clip.out_t, src.fps, tol)
        out_shift = end - clip.out_t

    # Si el ajuste deja el clip vacio, se conserva al menos un GOP.
    if end <= start + tol:
        _, nxt = keyframe_bracket(src.path, start + tol, src.fps)
        end = nxt if nxt is not None else min(src.duration, start + 1.0)
        out_shift = end - clip.out_t

    return (Piece(clip, start, end, COPY),
            CutShift(clip.name, start - clip.in_t, out_shift))


def _nearest_keyframe(path: str, t: float, fps: float, tol: float) -> float:
    prev, nxt = keyframe_bracket(path, t, fps)
    if nxt is None:
        return prev
    if abs(prev - t) <= tol:
        return prev
    return prev if (t - prev) <= (nxt - t) else nxt


class ExportWorker(QThread):
    """Ejecuta el plan en segundo plano para no bloquear la interfaz."""

    progress = Signal(float, str)     # 0..1, mensaje
    finished_ok = Signal(str, str)    # ruta de salida, resumen
    failed = Signal(str)

    def __init__(self, timeline: Timeline, out_path: str,
                 mode: str = MODE_LOSSLESS, quality: int = 18,
                 encoder: str = "h264_nvenc", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.tl = timeline
        self.out_path = Path(out_path)
        self.mode = mode
        self.quality = quality
        self.encoder = encoder
        self._cancel = False
        self._proc = None
        self.tmp_dir: Path | None = None

    def cancel(self) -> None:
        self._cancel = True
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
            except Exception:
                pass

    # ---------- ejecucion ----------

    def run(self) -> None:  # pragma: no cover - hilo
        try:
            self._export()
        except Exception as exc:
            if not self._cancel:
                self.failed.emit(str(exc))
        finally:
            self._cleanup()

    def _export(self) -> None:
        self.progress.emit(0.0, "Analizando cortes y buscando keyframes...")
        label = {"h264_nvenc": "NVENC H.264", "hevc_nvenc": "NVENC HEVC",
                 "libx264": "libx264"}.get(self.encoder, self.encoder)
        plan = build_plan(self.tl, self.mode, encoder_label=label)
        if not plan.pieces:
            raise FFmpegError("No hay nada que exportar: la linea de tiempo esta vacia.")

        # El temporal va junto al destino para que quede en el mismo disco y el
        # movimiento final no implique copiar decenas de GB entre unidades.
        # mkdtemp y no un nombre derivado del reloj: crea el directorio con
        # O_EXCL y sufijo aleatorio, asi que nadie ha podido crearlo antes para
        # que los fragmentos acaben escritos dentro de algo suyo. Importa porque
        # el destino puede ser una carpeta compartida donde escribe mas gente.
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.tmp_dir = Path(tempfile.mkdtemp(prefix=".swtmp_",
                                             dir=str(self.out_path.parent)))

        total = plan.total_duration
        done = 0.0
        parts: list[Path] = []

        for i, piece in enumerate(plan.pieces):
            if self._cancel:
                return
            part = self.tmp_dir / f"p{i:04d}{PIECE_EXT}"
            label = "copiando" if piece.mode == COPY else "recodificando"
            self._run_piece(piece, plan, part, done, total,
                            f"Parte {i + 1}/{len(plan.pieces)} ({label})")
            if not part.exists() or part.stat().st_size == 0:
                raise FFmpegError(
                    f"El fragmento {i + 1} salio vacio ({piece.mode}, "
                    f"{piece.start:.3f}-{piece.end:.3f} de {piece.clip.name}).")
            parts.append(part)
            done += piece.out_duration

        if self._cancel:
            return

        audio = None
        if plan.any_audio and not plan.copy_audio:
            self.progress.emit(0.95, "Montando la pista de audio...")
            audio = self._build_audio(plan)

        if self._cancel:
            return

        self.progress.emit(0.97, "Uniendo todo en el archivo final...")
        self._concat(parts, plan, audio)

        if self._cancel:
            return

        note = self._verify(plan)
        self.progress.emit(1.0, "Listo")
        summary = plan.summary()
        if plan.warnings:
            summary += "\n\n" + "\n".join("· " + w for w in plan.warnings)
        if note:
            summary += "\n\n" + note
        self.finished_ok.emit(str(self.out_path), summary)

    # ---------- fragmentos ----------

    def _run_piece(self, piece: Piece, plan: Plan, out: Path,
                   done_before: float, total: float, label: str) -> None:
        src = piece.clip.source
        head = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
                "-progress", "pipe:1", "-nostats"]
        inputs: list[str] = []
        outputs: list[str] = []

        if piece.mode == COPY:
            # start es exactamente un keyframe, asi que el seek cae clavado.
            #
            # El final se acota por numero de paquetes y no por tiempo: con
            # B-frames el orden de decodificacion no es el de presentacion, y -t
            # se mide sobre el primero, asi que arrastra un par de fotogramas de
            # mas en cada corte. -frames:v cuenta paquetes y no admite deriva.
            # El -t que lo acompania solo esta para cerrar la pista de audio.
            inputs += ["-ss", f"{piece.start:.6f}", "-i", as_input(src.path)]
            outputs += ["-frames:v", str(piece.frames),
                        "-t", f"{piece.out_duration:.6f}",
                        "-map", "0:v:0", "-c:v", "copy"]
        else:
            # -noautorotate: los tramos copiados salen en su orientacion cruda, asi
            # que los recodificados deben salir igual y rotarse solo si toca.
            inputs += ["-noautorotate", "-ss", f"{piece.start:.6f}",
                       "-i", as_input(src.path)]
            # Numero de fotogramas en vez de duracion: no admite ambiguedad.
            outputs += ["-frames:v", str(piece.frames), "-map", "0:v:0"]
            vf = _rotation_filter(piece.delta_rot)
            if (src.width, src.height) != (plan.target_w, plan.target_h) or piece.delta_rot:
                vf.append(
                    f"scale={plan.target_w}:{plan.target_h}"
                    ":force_original_aspect_ratio=decrease")
                vf.append(f"pad={plan.target_w}:{plan.target_h}:(ow-iw)/2:(oh-ih)/2")
                vf.append("setsar=1")
            if vf:
                outputs += ["-vf", ",".join(vf)]
            outputs += self._encoder_args(src) + ["-r", f"{plan.fps:.6f}"]

        # El audio solo viaja dentro de los trozos cuando se copia tal cual; si hay
        # que recodificarlo se hace en una sola pasada aparte (ver _build_audio).
        if plan.copy_audio and piece.clip.source.has_audio:
            outputs += ["-map", "0:a:0", "-c:a", "copy"]
        else:
            outputs += ["-an"]

        args = head + inputs + outputs
        args += ["-avoid_negative_ts", "make_zero", "-f", PIECE_FORMAT,
                 "-y", str(out)]
        # El video ocupa el 90% de la barra; el audio y la union, el resto.
        span = 0.90 * piece.out_duration / max(total, 0.001)
        self._spawn(args, piece.out_duration, label,
                    base=0.90 * done_before / max(total, 0.001), span=span)

    def _encoder_args(self, src) -> list[str]:
        """NVENC si puede con el formato; libx264 en caso contrario.

        quality = 0 significa 'ajustar al bitrate del original'. Es el valor por
        defecto porque un QP fijo alto duplica el tamanio del archivo, y con
        grabaciones de 20 GB eso son 20 GB de mas por cada exportacion.
        """
        eight_bit = src.pix_fmt in ("yuv420p", "nv12", "yuvj420p")
        bitrate = src.vbitrate if src.vbitrate > 0 else 0

        if self.encoder.endswith("nvenc") and eight_bit:
            # 'high' es un perfil de H.264; HEVC no lo reconoce y falla al abrir.
            profile = "main" if "hevc" in self.encoder else "high"
            args = ["-c:v", self.encoder, "-preset", "p6", "-tune", "hq",
                    "-profile:v", profile, "-pix_fmt", "yuv420p", "-bf", "2"]
            if self.quality <= 0 and bitrate:
                # El techo manda: con -cq suelto, NVENC se va hasta el maxrate y
                # el archivo acaba pesando casi el doble que el original.
                cap = int(bitrate * 1.35)
                args += ["-rc", "vbr", "-cq", "21", "-b:v", str(bitrate),
                         "-maxrate", str(cap), "-bufsize", str(cap * 2)]
            else:
                args += ["-rc", "constqp", "-qp", str(self.quality or 20)]
            return args

        args = ["-c:v", "libx264", "-preset", "medium",
                "-pix_fmt", src.pix_fmt if eight_bit else "yuv420p"]
        if self.quality <= 0 and bitrate:
            cap = int(bitrate * 1.35)
            args += ["-crf", "20", "-maxrate", str(cap), "-bufsize", str(cap * 2)]
        else:
            args += ["-crf", str(max(0, (self.quality or 20) - 2))]
        return args

    def _build_audio(self, plan: Plan) -> Path:
        """Genera TODA la pista de audio en una sola pasada de ffmpeg.

        Codificar el audio trozo a trozo parece lo natural, pero cada arranque del
        codificador AAC anade unas milesimas de relleno al principio; con quince
        cortes eso son casi medio segundo de desfase acumulado. Concatenando en un
        unico filtro solo hay un arranque, y ademas desaparecen los clics en las
        uniones.
        """
        out = self.tmp_dir / "audio.m4a"
        inputs: list[str] = []
        chains: list[str] = []
        labels: list[str] = []

        for i, (clip, dur) in enumerate(plan.clip_durations()):
            src = clip.source
            if src.has_audio:
                inputs += ["-ss", f"{clip.in_t:.6f}", "-t", f"{dur:.6f}",
                           "-i", as_input(src.path)]
            else:
                inputs += ["-f", "lavfi", "-t", f"{dur:.6f}",
                           "-i", "anullsrc=r=48000:cl=stereo"]
            # Igualar formato y ritmo antes de concatenar: el filtro concat exige
            # que todas las entradas coincidan.
            chains.append(
                f"[{i}:a]aresample=48000:async=1,"
                f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                f"atrim=duration={dur:.6f},asetpts=N/SR/TB[a{i}]")
            labels.append(f"[a{i}]")

        graph = ";".join(chains)
        if len(labels) > 1:
            graph += ";" + "".join(labels) + f"concat=n={len(labels)}:v=0:a=1[aout]"
            final = "[aout]"
        else:
            final = labels[0]

        args = ([FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
                 "-progress", "pipe:1", "-nostats"] + inputs +
                ["-filter_complex", graph, "-map", final,
                 "-c:a", "aac", "-b:a", "320k", "-ar", "48000", "-ac", "2",
                 "-y", str(out)])
        self._spawn(args, plan.total_duration, "Montando la pista de audio...",
                    base=0.90, span=0.06)
        if not out.exists() or out.stat().st_size == 0:
            raise FFmpegError("No se pudo generar la pista de audio.")
        return out

    def _spawn(self, args: list[str], duration: float, label: str,
               base: float, span: float) -> None:
        """Lanza ffmpeg y traduce su progreso al tramo [base, base+span] global."""
        self._proc = popen(args)
        tail: list[str] = []
        for line in self._proc.stdout:
            if self._cancel:
                return
            line = line.strip()
            if line.startswith("out_time_ms="):
                try:
                    secs = int(line.split("=", 1)[1]) / 1_000_000.0
                except ValueError:
                    continue
                local = min(1.0, secs / max(duration, 0.001))
                self.progress.emit(min(0.999, base + local * span), label)
            elif line and not re.match(r"^[a-z_]+=", line):
                tail.append(line)
                del tail[:-12]
        code = self._proc.wait()
        if code != 0 and not self._cancel:
            raise FFmpegError(f"ffmpeg fallo ({label}):\n" + "\n".join(tail[-8:]))

    # ---------- union final ----------

    def _concat(self, parts: list[Path], plan: Plan, audio: Path | None) -> None:
        listing = self.tmp_dir / "concat.txt"
        # El demuxer concat lee este archivo linea a linea y trata cada linea
        # como una directiva. La ruta viene del directorio que eligio el usuario:
        # una comilla simple en el nombre de la carpeta cerraria el literal y el
        # resto de la ruta se leeria como opciones del demuxer.
        listing.write_text(
            "".join("file " + _concat_quote(part.as_posix()) + LF for part in parts),
            encoding="utf-8")

        args = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-fflags", "+genpts",
                "-i", str(listing)]
        if audio is not None:
            args += ["-i", str(audio)]
        args += ["-map", "0:v:0", "-c", "copy"]
        if audio is not None:
            args += ["-map", "1:a:0"]
        elif plan.copy_audio:
            args += ["-map", "0:a:0"]
        if plan.out_rotation:
            # Los trozos TS no llevan matriz de visualizacion, asi que la rotacion
            # se escribe explicitamente aqui. Es metadato: cero recodificacion.
            args += ["-metadata:s:v:0", f"rotate={plan.out_rotation % 360}"]
        args += ["-movflags", "+faststart", "-y", str(self.out_path)]

        res = run(args, timeout=None)
        if res.returncode != 0:
            raise FFmpegError("Fallo al unir los fragmentos:\n" + (res.stderr or "")[-800:])

        if plan.out_rotation:
            self._ensure_rotation(plan.out_rotation)

    def _ensure_rotation(self, rotation: int) -> None:
        """Verifica la rotacion escrita y la corrige si el muxer la ignoro."""
        try:
            got = probe(str(self.out_path)).rotation
        except Exception:
            return
        if got == rotation % 360:
            return
        fixed = self.out_path.with_name(self.out_path.stem + ".rot.mp4")
        res = run([FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
                   "-display_rotation", f"{-(rotation % 360)}",
                   "-i", as_input(self.out_path), "-c", "copy",
                   "-movflags", "+faststart", "-y", str(fixed)], timeout=None)
        if res.returncode == 0 and fixed.exists() and fixed.stat().st_size > 0:
            self.out_path.unlink(missing_ok=True)
            fixed.replace(self.out_path)

    def _verify(self, plan: Plan) -> str:
        """Verifica duracion y, sobre todo, que la imagen no se corrompa en los cortes."""
        notes: list[str] = []
        try:
            got = probe(str(self.out_path))
        except Exception as exc:
            return f"Aviso: no se pudo verificar el archivo exportado ({exc})."

        drift = abs(got.duration - plan.total_duration)
        if drift > 1.0:
            notes.append(
                f"Aviso: la duracion final ({got.duration:.2f} s) difiere "
                f"{drift:.2f} s de la esperada ({plan.total_duration:.2f} s).")

        bad = self._check_junctions(plan)
        if bad:
            notes.append(
                f"AVISO IMPORTANTE: se detectaron errores de imagen en {bad} de los "
                "cortes. Vuelve a exportar en modo 'Exacto al fotograma' y avisa "
                "de que esto ha pasado.")
        return "\n\n".join(notes)

    def _check_junctions(self, plan: Plan) -> int:
        """Decodifica unos segundos alrededor de cada union y cuenta las malas.

        Esta comprobacion existe porque una version anterior producia justo ahi
        imagen congelada, y el archivo parecia correcto por duracion y por numero
        de fotogramas. Solo decodificando se ve. Son unos segundos por corte, no
        el archivo entero, asi que es barato incluso con material de 20 GB.
        """
        bounds: list[float] = []
        acc = 0.0
        for piece in plan.pieces[:-1]:
            acc += piece.out_duration
            bounds.append(acc)
        if not bounds:
            return 0

        bad = 0
        for t in bounds[:12]:      # con doce muestras basta para detectar el patron
            if self._cancel:
                break
            res = run([FFMPEG, "-hide_banner", "-v", "error", "-nostdin",
                       "-ss", f"{max(0.0, t - 2.0):.3f}", "-i", as_input(self.out_path),
                       "-t", "4", "-f", "null", "-"], timeout=300)
            errors = [x for x in (res.stderr or "").splitlines() if x.strip()]
            # Un par de avisos sueltos son normales al empezar a decodificar a
            # mitad de un GOP; decenas significan imagen rota.
            if len(errors) > 5:
                bad += 1
        return bad

    def _cleanup(self) -> None:
        if self.tmp_dir and self.tmp_dir.exists():
            shutil.rmtree(self.tmp_dir, ignore_errors=True)


def _rotation_filter(delta: int) -> list[str]:
    delta %= 360
    if delta == 90:
        return ["transpose=1"]
    if delta == 180:
        return ["transpose=1", "transpose=1"]
    if delta == 270:
        return ["transpose=2"]
    return []
