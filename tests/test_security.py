"""Pruebas de regresion de las defensas de entrada.

Cada bloque corresponde a un hallazgo de la auditoria documentada en
SECURITY.md. La idea es que si alguien retira una de las defensas mas adelante,
aqui se note en vez de descubrirlo cuando ya este publicado.

No hacen falta muestras de video: todo lo que se prueba ocurre antes de que
ffmpeg llegue a decodificar nada.
"""
from __future__ import annotations

import json
import shlex
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.exporter import _concat_quote  # noqa: E402
from app.ffmpeg_tools import (UnsafePathError, as_input,  # noqa: E402
                              safe_path, verify_binaries)
from app.model import MAX_CLIPS, ProjectError, Timeline  # noqa: E402

SAMPLES = Path(__file__).resolve().parent / "samples"
LAND30 = SAMPLES / "landscape_30s.mp4"

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


def rejects(name: str, value, fn=safe_path, exc=UnsafePathError) -> None:
    """Comprueba que fn(value) se niega en vez de dejar pasar el valor."""
    try:
        fn(value)
    except exc:
        check(name, True)
    except Exception as other:
        check(name, False, f"se rechazo, pero con {type(other).__name__}: {other}")
    else:
        check(name, False, "se acepto un valor que deberia rechazarse")


# ---------------------------------------------------------------- rutas

def test_safe_path() -> None:
    print("\n[1] safe_path: solo archivos locales reales llegan a ffmpeg")

    # ffmpeg trata el nombre de entrada como una URL. Estos son nombres validos
    # para ffmpeg y no deben pasar de aqui.
    rejects("rechaza http://", "http://example.invalid/video.mp4")
    rejects("rechaza el protocolo concat:", "concat:a.ts|b.ts")
    rejects("rechaza el protocolo subfile:", "subfile:,start,0,end,99,,:C:/x.mp4")
    rejects("rechaza el protocolo crypto:", "crypto:C:/x.mp4")
    rejects("rechaza una entrada de lavfi", "anullsrc=r=48000")

    rejects("rechaza la cadena vacia", "")
    rejects("rechaza solo espacios", "   ")
    rejects("rechaza una ruta con salto de linea", "C:/video.mp4" + chr(10) + "file:/etc")
    rejects("rechaza una ruta con byte nulo", "C:/video" + chr(0) + ".mp4")
    rejects("rechaza una ruta absurdamente larga", "C:/" + ("a" * 5000) + ".mp4")
    rejects("rechaza algo que no es texto", 1234)
    rejects("rechaza un archivo inexistente", "C:/no/existe/jamas_12345.mp4")
    rejects("rechaza un directorio", str(SAMPLES))

    if LAND30.is_file():
        got = safe_path(str(LAND30))
        check("acepta un video real y lo devuelve absoluto",
              got == LAND30.resolve(), str(got))
        # Una ruta relativa con .. sigue siendo valida si resuelve a un archivo:
        # lo que importa es donde acaba, no como se escribio.
        rel = str(SAMPLES / ".." / "samples" / LAND30.name)
        check("normaliza los .. intermedios", safe_path(rel) == LAND30.resolve())


def test_as_input() -> None:
    print("\n[2] as_input: el nombre no se puede reinterpretar como opcion")

    with tempfile.TemporaryDirectory() as tmp:
        # ffprobe recibe el archivo en posicion final, donde un nombre que
        # empieza por guion se parsea como opcion.
        tricky = Path(tmp) / "-loglevel.mp4"
        tricky.write_bytes(b"x")
        arg = as_input(tricky)
        check("prefija el protocolo file:", arg.startswith("file:"), arg)
        check("un nombre con guion inicial deja de parecer una opcion",
              not arg.startswith("-"), arg)
        check("la ruta sale absoluta", ":" in arg[5:], arg)


# ------------------------------------------------- lista del demuxer concat

def test_concat_quote() -> None:
    print("\n[3] _concat_quote: la lista de concat no se puede inyectar")

    q = chr(39)          # comilla simple
    bs = chr(92)

    plain = _concat_quote("C:/salida/p0000.mkv")
    check("cita una ruta normal", plain == q + "C:/salida/p0000.mkv" + q, plain)

    # Una carpeta de destino llamada  it's mine  cerraria el literal y el resto
    # de la ruta se leeria como directivas del demuxer.
    got = _concat_quote("C:/it" + q + "s/p0.mkv")
    want = q + "C:/it" + q + bs + q + q + "s/p0.mkv" + q
    check("escapa la comilla simple al estilo POSIX", got == want, got)

    # La prueba que de verdad importa: volver a parsear lo citado tiene que
    # devolver la ruta original y una sola palabra. Si algo se escapara mal,
    # el parser veria dos tokens y el segundo serian directivas del demuxer.
    for raw in ("C:/salida/p0.mkv",
                "C:/it" + q + "s/p0.mkv",
                "C:/dos" + q + q + "seguidas/p0.mkv",
                "C:/con espacios/p0.mkv",
                "C:/raro " + q + "; file /etc/passwd/p0.mkv"):
        tokens = shlex.split(_concat_quote(raw), posix=True)
        check(f"round-trip exacto de {raw!r}",
              tokens == [raw], str(tokens))


# ------------------------------------------------------- archivos .swproj

def write_project(tmp: Path, payload) -> str:
    prj = tmp / "malo.swproj"
    if isinstance(payload, str):
        prj.write_text(payload, encoding="utf-8")
    else:
        prj.write_text(json.dumps(payload), encoding="utf-8")
    return str(prj)


def test_project_validation() -> None:
    print("\n[4] .swproj: se trata como entrada no confiable")

    tl = Timeline()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)

        def bad(name: str, payload) -> None:
            rejects(name, write_project(tmp, payload), fn=tl.load, exc=ProjectError)

        bad("rechaza JSON invalido", "{ esto no es json")
        bad("rechaza un JSON que no es un objeto", [1, 2, 3])
        bad("rechaza una version desconocida", {"version": 99, "clips": []})
        bad("rechaza que falte la version", {"clips": []})
        bad("rechaza clips que no son una lista", {"version": 1, "clips": "todos"})
        bad("rechaza un clip que no es un objeto",
            {"version": 1, "clips": ["C:/x.mp4"]})
        bad("rechaza un clip sin ruta",
            {"version": 1, "clips": [{"in": 0, "out": 1}]})
        bad("rechaza una ruta que no es texto",
            {"version": 1, "clips": [{"path": 7, "in": 0, "out": 1}]})
        bad("rechaza una rotacion arbitraria",
            {"version": 1, "clips": [{"path": "C:/x.mp4", "in": 0, "out": 1,
                                      "rotate": 45}]})
        bad("rechaza un intervalo invertido",
            {"version": 1, "clips": [{"path": "C:/x.mp4", "in": 5, "out": 1}]})
        bad("rechaza un inicio negativo",
            {"version": 1, "clips": [{"path": "C:/x.mp4", "in": -3, "out": 1}]})
        bad("rechaza un tiempo que no es numero",
            {"version": 1, "clips": [{"path": "C:/x.mp4", "in": "0", "out": 1}]})

        # json.loads acepta NaN e Infinity aunque no esten en el estandar, y una
        # duracion no finita envenena en silencio todos los calculos posteriores.
        bad("rechaza NaN como tiempo",
            '{"version": 1, "clips": [{"path": "C:/x.mp4", "in": NaN, "out": 1}]}')
        bad("rechaza Infinity como duracion",
            '{"version": 1, "clips": [{"path": "C:/x.mp4", "in": 0, '
            '"out": Infinity}]}')

        # Limites de recursos: un proyecto no puede pedir trabajo ilimitado.
        bad("rechaza mas clips de los permitidos",
            {"version": 1, "clips": [{"path": "C:/x.mp4", "in": 0, "out": 1}]
                                    * (MAX_CLIPS + 1)})

        gordo = tmp / "gordo.swproj"
        gordo.write_text('{"version": 1, "relleno": "' + "a" * (9 * 1024 * 1024) +
                         '", "clips": []}', encoding="utf-8")
        rejects("rechaza un proyecto gigante antes de parsearlo", str(gordo),
                fn=tl.load, exc=ProjectError)

        # Un video que ya no esta no es un ataque: se avisa y se sigue.
        faltan = tl.load(write_project(tmp, {
            "version": 1,
            "clips": [{"path": "C:/no/existe/jamas_98765.mp4", "in": 0, "out": 1}],
        }))
        check("un archivo que falta se reporta, no revienta",
              faltan == ["C:/no/existe/jamas_98765.mp4"], str(faltan))

        # Y un proyecto correcto sigue cargando.
        if LAND30.is_file():
            ok_prj = write_project(tmp, {
                "version": 1,
                "clips": [{"path": str(LAND30), "in": 1.0, "out": 4.0,
                           "rotate": 90}],
            })
            check("un proyecto correcto carga sin quejas", tl.load(ok_prj) == [])
            check("y conserva el recorte", len(tl.clips) == 1 and
                  abs(tl.clips[0].duration - 3.0) < 0.001)


def test_clamp_to_real_duration() -> None:
    print("\n[5] .swproj: el recorte no puede pasarse del final real")

    if not LAND30.is_file():
        check("omitida (falta landscape_30s.mp4)", True)
        return

    tl = Timeline()
    with tempfile.TemporaryDirectory() as raw:
        prj = write_project(Path(raw), {
            "version": 1,
            "clips": [{"path": str(LAND30), "in": 0.0, "out": 9_000_000.0}],
        })
        tl.load(prj)
        real = tl.clips[0].source.duration
        check("un 'out' enorme se recorta a la duracion del archivo",
              abs(tl.clips[0].out_t - real) < 0.001,
              f"out={tl.clips[0].out_t} duracion={real}")


# ---------------------------------------------------- cadena de suministro

def test_binaries() -> None:
    print("\n[6] bin/: los binarios de terceros estan verificados")

    problems = verify_binaries()
    check("ffmpeg, ffprobe y libmpv coinciden con SHA256SUMS",
          not problems, "; ".join(problems))

    manifest = Path(__file__).resolve().parent.parent / "bin" / "SHA256SUMS"
    check("el manifiesto existe y esta versionado", manifest.is_file())
    if manifest.is_file():
        lines = [l for l in manifest.read_text(encoding="utf-8").splitlines() if l.strip()]
        check("cubre los tres binarios", len(lines) == 3, str(len(lines)))


def main() -> int:
    print("=" * 64)
    print("Regresion de seguridad (ver SECURITY.md)")
    print("=" * 64)

    for fn in (test_safe_path, test_as_input, test_concat_quote,
               test_project_validation, test_clamp_to_real_duration,
               test_binaries):
        try:
            fn()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            _fails.append(f"{fn.__name__}: excepcion {exc}")

    print("\n" + "=" * 64)
    print(f"{_passes} comprobaciones correctas, {len(_fails)} fallos")
    for f in _fails:
        print("  FAIL " + f)
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
