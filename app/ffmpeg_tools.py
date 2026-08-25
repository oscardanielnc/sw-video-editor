"""Envoltorio sobre ffmpeg/ffprobe.

Regla de oro de este modulo: nunca decodificar el archivo completo. Todo lo que
se hace aqui lee cabeceras o hace seek directo, para que un MP4 de 20 GB abra
igual de rapido que uno de 20 MB.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
CACHE = ROOT / "cache"
FFMPEG = str(BIN / "ffmpeg.exe")
FFPROBE = str(BIN / "ffprobe.exe")

# Evita que aparezca una ventana negra de consola por cada proceso lanzado.
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class FFmpegError(RuntimeError):
    pass


class UnsafePathError(FFmpegError):
    """La ruta no es un archivo local normal y no se le pasa a ffmpeg."""


# ffmpeg resuelve el nombre de entrada como una URL: 'http://', 'concat:',
# 'subfile:', 'crypto:' y una veintena mas de protocolos son nombres validos.
# Un .swproj de otra persona lleva rutas dentro, asi que esa cadena es entrada
# no confiable y no puede llegar cruda a la linea de comandos.
_MAX_PATH_LEN = 4096


def safe_path(path: str) -> Path:
    """Normaliza y valida una ruta de medios antes de dejarla tocar ffmpeg.

    Rechaza lo que no sea un archivo local existente: asi 'http://host/x' o
    'concat:a|b' no llegan nunca al proceso hijo, ni siquiera cuando vienen de
    un archivo de proyecto ajeno.
    """
    if not isinstance(path, str) or not path.strip():
        raise UnsafePathError("Ruta de video vacia.")
    if len(path) > _MAX_PATH_LEN:
        raise UnsafePathError("Ruta de video demasiado larga.")
    if any(ord(ch) < 32 for ch in path):
        raise UnsafePathError("La ruta contiene caracteres de control.")
    try:
        resolved = Path(path).resolve(strict=True)
    except OSError as exc:
        raise UnsafePathError(f"No se puede acceder a la ruta: {exc}") from exc
    if not resolved.is_file():
        raise UnsafePathError(f"'{resolved}' no es un archivo normal.")
    return resolved


def as_input(path: str | Path) -> str:
    """Ruta lista para pasarsela a ffmpeg como entrada.

    El prefijo 'file:' fija el protocolo explicitamente. Sin el, ffmpeg
    interpreta el nombre y una ruta que empiece por guion se cuela como opcion
    en las invocaciones donde el archivo va en posicion final (ffprobe).
    """
    return "file:" + str(Path(path).resolve()).replace("\\", "/")


MANIFEST = BIN / "SHA256SUMS"
REQUIRED_BINARIES = ("ffmpeg.exe", "ffprobe.exe", "libmpv-2.dll")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_binaries() -> list[str]:
    """Comprueba bin/ contra SHA256SUMS. Devuelve los problemas encontrados.

    ffmpeg, ffprobe y libmpv son 300 MB de codigo de terceros que se descargan a
    mano y que este programa ejecuta con los permisos de quien lo usa. Sin un
    manifiesto, nadie que clone el repositorio puede distinguir la descarga
    correcta de otra cosa. El manifiesto se versiona; los binarios no.
    """
    problems: list[str] = []
    expected: dict[str, str] = {}
    if MANIFEST.is_file():
        for line in MANIFEST.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2:
                expected[parts[1]] = parts[0].lower()
    else:
        problems.append(
            "Falta bin/SHA256SUMS: no se puede comprobar la integridad de los "
            "binarios incluidos.")

    for name in REQUIRED_BINARIES:
        target = BIN / name
        if not target.is_file():
            problems.append(f"Falta bin/{name}.")
            continue
        want = expected.get(name)
        if want and _sha256(target) != want:
            problems.append(
                f"bin/{name} no coincide con el hash publicado en SHA256SUMS. "
                f"No lo uses: vuelve a descargarlo desde el origen documentado "
                f"en el README.")
    return problems


def run(args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    """Lanza un hijo con la lista de argumentos tal cual: nunca shell, nunca PATH.

    args[0] siempre es una ruta absoluta a bin/, asi que no hay busqueda por PATH
    ni riesgo de que un ejecutable plantado en el directorio de trabajo gane la
    resolucion. stdin se cierra para que ffmpeg no pueda quedarse esperando a que
    alguien conteste a un prompt.
    """
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )


def popen(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        stdin=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )


def _frac(value: str | None, default: float = 0.0) -> float:
    if not value:
        return default
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else default
        return float(value)
    except (ValueError, ZeroDivisionError):
        return default


@dataclass
class Source:
    """Metadatos de un archivo de video importado. Se obtienen solo de cabeceras."""

    path: str
    duration: float
    width: int
    height: int
    fps: float
    vcodec: str
    pix_fmt: str
    profile: str
    vbitrate: int
    rotation: int          # rotacion ya presente en el contenedor (0/90/180/270)
    has_audio: bool
    acodec: str
    asample_rate: int
    achannels: int
    abitrate: int
    size_bytes: int
    time_base_fps: str = ""   # r_frame_rate original, para conservar exactitud

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def frame_dur(self) -> float:
        return 1.0 / self.fps if self.fps > 0 else 1.0 / 30.0

    def display_size(self, extra_rotation: int = 0) -> tuple[int, int]:
        """Dimensiones tal como se ven, considerando rotacion de contenedor + usuario."""
        if (self.rotation + extra_rotation) % 180 == 90:
            return self.height, self.width
        return self.width, self.height

    def signature(self) -> tuple:
        """Dos fuentes con la misma firma pueden concatenarse sin recodificar."""
        return (self.vcodec, self.width, self.height, self.pix_fmt,
                self.profile, round(self.fps, 3))


def probe(path: str) -> Source:
    """Lee metadatos del archivo. Instantaneo incluso en archivos de decenas de GB."""
    real = safe_path(path)
    res = run([
        FFPROBE, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", as_input(real),
    ], timeout=60)
    if res.returncode != 0:
        raise FFmpegError(f"ffprobe fallo en {real.name}:\n{res.stderr.strip()}")

    data = json.loads(res.stdout)
    streams = data.get("streams", [])
    fmt = data.get("format", {})

    vs = next((s for s in streams if s.get("codec_type") == "video"), None)
    if vs is None:
        raise FFmpegError(f"{Path(path).name} no contiene pista de video.")
    aud = next((s for s in streams if s.get("codec_type") == "audio"), None)

    # La duracion del contenedor es mas fiable que la del stream en grabaciones largas.
    duration = _frac(fmt.get("duration")) or _frac(vs.get("duration"))
    if duration <= 0:
        raise FFmpegError(f"No se pudo determinar la duracion de {Path(path).name}.")

    r_fps = vs.get("avg_frame_rate") or vs.get("r_frame_rate") or "30/1"
    fps = _frac(r_fps, 30.0)
    if fps <= 0:
        fps = _frac(vs.get("r_frame_rate"), 30.0) or 30.0

    return Source(
        path=str(real),
        duration=duration,
        width=int(vs.get("width") or 0),
        height=int(vs.get("height") or 0),
        fps=fps,
        vcodec=vs.get("codec_name", "?"),
        pix_fmt=vs.get("pix_fmt", "yuv420p"),
        profile=str(vs.get("profile", "")),
        vbitrate=int(_frac(vs.get("bit_rate")) or _frac(fmt.get("bit_rate")) or 0),
        rotation=_container_rotation(vs),
        has_audio=aud is not None,
        acodec=(aud or {}).get("codec_name", ""),
        asample_rate=int(_frac((aud or {}).get("sample_rate"), 48000)),
        achannels=int((aud or {}).get("channels") or 2),
        abitrate=int(_frac((aud or {}).get("bit_rate")) or 0),
        size_bytes=int(_frac(fmt.get("size"))),
        time_base_fps=str(vs.get("r_frame_rate") or r_fps),
    )


def _container_rotation(vstream: dict) -> int:
    """Rotacion de visualizacion en grados horarios, 0/90/180/270."""
    tag = (vstream.get("tags") or {}).get("rotate")
    if tag is not None:
        try:
            return int(round(float(tag))) % 360
        except ValueError:
            pass
    for sd in vstream.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                # La display matrix reporta la rotacion en sentido antihorario.
                return int(round(-float(sd["rotation"]))) % 360
            except ValueError:
                pass
    return 0


def keyframes_near(path: str, t: float, back: float = 20.0,
                   fwd: float = 20.0) -> list[float]:
    """Keyframes de video en la ventana [t-back, t+fwd].

    Se usa -read_intervals para que ffprobe solo demuxee ese tramo: en un archivo
    de 20 GB tarda decimas de segundo en vez de varios minutos.
    """
    start = max(0.0, t - back)
    span = back + fwd
    res = run([
        FFPROBE, "-v", "error",
        "-read_intervals", f"{start:.6f}%+{span:.6f}",
        "-select_streams", "v:0", "-skip_frame", "nokey",
        "-show_entries", "frame=pts_time,best_effort_timestamp_time",
        "-print_format", "json", as_input(path),
    ], timeout=180)
    if res.returncode != 0:
        return []

    times: list[float] = []
    for fr in json.loads(res.stdout or "{}").get("frames", []):
        raw = fr.get("pts_time") or fr.get("best_effort_timestamp_time")
        if raw in (None, "N/A"):
            continue
        try:
            times.append(float(raw))
        except ValueError:
            continue
    return sorted(set(times))


def keyframe_bracket(path: str, t: float, fps: float) -> tuple[float, float | None]:
    """Devuelve (keyframe <= t, siguiente keyframe > t).

    Amplia la ventana de busqueda si hace falta, porque algunas grabaciones usan
    GOPs muy largos (10 s o mas).
    """
    tol = 0.5 / fps if fps > 0 else 0.016
    for back, fwd in ((20.0, 20.0), (60.0, 60.0), (180.0, 180.0)):
        kfs = keyframes_near(path, t, back, fwd)
        if not kfs:
            continue
        prev = [k for k in kfs if k <= t + tol]
        nxt = [k for k in kfs if k > t + tol]
        # Solo confiamos si la ventana realmente cubrio el punto por ambos lados,
        # o si ya llegamos al inicio del archivo.
        if prev and (nxt or t - back <= 0):
            return prev[-1], (nxt[0] if nxt else None)
        if prev and not nxt:
            return prev[-1], None
    return 0.0, None


def extract_thumb(path: str, t: float, height: int, out_path: Path) -> bool:
    """Extrae un fotograma suelto usando seek rapido por keyframe."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    res = run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{max(0.0, t):.3f}", "-i", as_input(path),
        "-frames:v", "1", "-vf", f"scale=-2:{height}",
        "-q:v", "5", "-y", str(out_path),
    ], timeout=60)
    return res.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0


def has_nvenc() -> bool:
    res = run([FFMPEG, "-hide_banner", "-encoders"], timeout=30)
    return "h264_nvenc" in (res.stdout or "")


def human_size(nbytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if nbytes < 1024 or unit == "TB":
            return f"{nbytes:.1f} {unit}" if unit != "B" else f"{int(nbytes)} B"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


def timecode(seconds: float, fps: float = 0.0) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if fps > 0:
        f = int(round((seconds - int(seconds)) * fps))
        if f >= int(round(fps)):
            f = int(round(fps)) - 1
        return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"
    return f"{h:02d}:{m:02d}:{s:02d}.{int((seconds % 1) * 1000):03d}"
