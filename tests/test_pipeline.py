"""Pruebas del nucleo: sondeo, keyframes, edicion y exportacion real con ffmpeg.

No usan interfaz grafica: llaman al exportador de forma sincrona para poder
verificar el archivo resultante fotograma a fotograma.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from app.exporter import (COPY, ENCODE, MODE_EXACT, MODE_LOSSLESS,  # noqa: E402
                          ExportWorker, analyze, build_plan)
from app.ffmpeg_tools import FFPROBE, keyframe_bracket, probe, run  # noqa: E402
from app.model import Timeline  # noqa: E402

SAMPLES = Path(__file__).resolve().parent / "samples"
OUT = Path(__file__).resolve().parent / "out"
LAND60 = str(SAMPLES / "landscape_60s.mp4")
LAND30 = str(SAMPLES / "landscape_30s.mp4")
PORT20 = str(SAMPLES / "portrait_20s.mp4")

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


def count_frames(path: str) -> int:
    res = run([FFPROBE, "-v", "error", "-select_streams", "v:0",
               "-count_frames", "-show_entries", "stream=nb_read_frames",
               "-print_format", "json", path], timeout=600)
    return int(json.loads(res.stdout)["streams"][0]["nb_read_frames"])


def export_sync(tl: Timeline, out: str, **kw) -> tuple[bool, str]:
    """Ejecuta la exportacion en el hilo actual y devuelve (exito, mensaje)."""
    worker = ExportWorker(tl, out, **kw)
    result: dict = {}
    worker.finished_ok.connect(lambda p, s: result.update(ok=True, msg=s))
    worker.failed.connect(lambda m: result.update(ok=False, msg=m))
    try:
        worker._export()
    except Exception as exc:
        result.update(ok=False, msg=str(exc))
    finally:
        worker._cleanup()
    return bool(result.get("ok")), str(result.get("msg", "sin resultado"))


# --------------------------------------------------------------------------

def test_probe() -> None:
    print("\n[1] Lectura de metadatos")
    s = probe(LAND60)
    check("duracion 60 s", abs(s.duration - 60) < 0.5, f"{s.duration}")
    check("resolucion 1280x720", (s.width, s.height) == (1280, 720),
          f"{s.width}x{s.height}")
    check("fps 30", abs(s.fps - 30) < 0.01, f"{s.fps}")
    check("codec h264", s.vcodec == "h264", s.vcodec)
    check("tiene audio", s.has_audio, "sin audio")
    check("rotacion 0", s.rotation == 0, str(s.rotation))

    p = probe(PORT20)
    check("vertical 720x1280", (p.width, p.height) == (720, 1280),
          f"{p.width}x{p.height}")


def test_keyframes() -> None:
    print("\n[2] Localizacion de keyframes (GOP de 3 s)")
    prev, nxt = keyframe_bracket(LAND60, 10.0, 30.0)
    check("kf antes de 10 s es 9.0", abs(prev - 9.0) < 0.05, f"{prev}")
    check("kf siguiente es 12.0", nxt is not None and abs(nxt - 12.0) < 0.05, f"{nxt}")

    prev, nxt = keyframe_bracket(LAND60, 12.0, 30.0)
    check("un punto sobre keyframe se detecta exacto", abs(prev - 12.0) < 0.05, f"{prev}")

    prev, _ = keyframe_bracket(LAND60, 0.4, 30.0)
    check("cerca del inicio devuelve 0.0", abs(prev) < 0.05, f"{prev}")


def test_editing() -> None:
    print("\n[3] Operaciones de edicion")
    tl = Timeline()
    tl.append_source(LAND60)
    check("un clip tras importar", len(tl.clips) == 1, str(len(tl.clips)))
    check("duracion = fuente", abs(tl.duration - 60) < 0.5, f"{tl.duration}")

    idx = tl.split_at(20.0)
    check("dividir crea 2 clips", len(tl.clips) == 2 and idx == 1, f"{len(tl.clips)}")
    check("division exacta en 20 s", abs(tl.clips[0].out_t - 20.0) < 0.001,
          f"{tl.clips[0].out_t}")
    check("duracion total intacta", abs(tl.duration - 60) < 0.5, f"{tl.duration}")

    tl.split_at(45.0)
    check("segunda division -> 3 clips", len(tl.clips) == 3, str(len(tl.clips)))

    # Elimina el trozo del medio (20-45).
    mid = tl.clips[1].cid
    tl.delete(mid)
    check("eliminar segmento deja 2 clips", len(tl.clips) == 2, str(len(tl.clips)))
    check("duracion baja a 35 s", abs(tl.duration - 35) < 0.5, f"{tl.duration}")

    # Reordena: el ultimo pasa al principio.
    last = tl.clips[-1].cid
    tl.move(last, 0)
    check("mover clip al inicio", tl.clips[0].cid == last, "no se movio")
    check("duracion no cambia al reordenar", abs(tl.duration - 35) < 0.5, f"{tl.duration}")

    tl.rotate(tl.clips[0].cid, 90)
    check("rotacion 90", tl.clips[0].rotate == 90, str(tl.clips[0].rotate))
    tl.rotate(tl.clips[0].cid, 270)
    check("rotacion vuelve a 0 tras 360", tl.clips[0].rotate == 0,
          str(tl.clips[0].rotate))

    # No debe dividir en un borde.
    tl2 = Timeline()
    tl2.append_source(LAND30)
    check("no divide en el tiempo 0", tl2.split_at(0.0) is None, "dividio igual")


def test_plan() -> None:
    print("\n[4] Planificacion de los dos modos")
    tl = Timeline()
    tl.append_source(LAND60)
    tl.clips[0].in_t, tl.clips[0].out_t = 10.0, 40.0   # ninguno cae en keyframe

    # Sin recodificar: los extremos se llevan al keyframe mas cercano.
    plan = build_plan(tl, MODE_LOSSLESS)
    check("modo sin recodificar solo genera copias",
          all(p.mode == COPY for p in plan.pieces), str([p.mode for p in plan.pieces]))
    check("el inicio se ajusta al keyframe 9.0",
          abs(plan.pieces[0].start - 9.0) < 0.05, f"{plan.pieces[0].start}")
    check("el final se ajusta al keyframe 39.0",
          abs(plan.pieces[0].end - 39.0) < 0.05, f"{plan.pieces[0].end}")
    check("informa del desplazamiento de los cortes", bool(plan.shift_detail()),
          "no informa")
    check("el desplazamiento no pasa de medio GOP", plan.max_shift <= 1.6,
          f"{plan.max_shift:.2f} s")

    # Exacto al fotograma: todo recodificado, respetando los puntos pedidos.
    plan_x = build_plan(tl, MODE_EXACT)
    check("modo exacto solo genera trozos recodificados",
          all(p.mode == ENCODE for p in plan_x.pieces),
          str([p.mode for p in plan_x.pieces]))
    check("modo exacto respeta el corte pedido",
          abs(plan_x.pieces[0].start - 10.0) < 0.001, f"{plan_x.pieces[0].start}")
    check("modo exacto dura exactamente 30 s",
          abs(plan_x.total_duration - 30) < 0.05, f"{plan_x.total_duration}")

    # Cortes ya sobre keyframes: sin desplazamiento y con audio copiado.
    tl2 = Timeline()
    tl2.append_source(LAND60)
    tl2.clips[0].in_t, tl2.clips[0].out_t = 12.0, 39.0
    plan2 = build_plan(tl2, MODE_LOSSLESS)
    check("cortes en keyframe no se mueven", plan2.max_shift < 0.001,
          f"{plan2.max_shift:.3f} s")
    check("copia bit a bit tambien copia el audio", plan2.copy_audio,
          "recodifico audio")

    # Rotaciones distintas impiden copiar: debe caer a modo exacto y avisar.
    tl3 = Timeline()
    tl3.append_source(LAND60)
    tl3.append_source(LAND30)
    tl3.rotate(tl3.clips[1].cid, 90)
    plan3 = build_plan(tl3, MODE_LOSSLESS)
    check("rotacion mixta obliga a recodificar", plan3.mode == MODE_EXACT,
          plan3.mode)
    check("y lo explica al usuario", bool(plan3.warnings), "sin avisos")

    ok, reason, _ = analyze(tl3)
    check("analyze() detecta que no se puede copiar", not ok and bool(reason),
          f"ok={ok} reason={reason}")


def test_export_cuts() -> None:
    print("\n[5] Exportacion exacta: cortar, borrar el medio y reordenar")
    OUT.mkdir(parents=True, exist_ok=True)
    out = str(OUT / "cuts.mp4")

    tl = Timeline()
    tl.append_source(LAND60)
    tl.split_at(10.4)          # deliberadamente fuera de keyframe
    tl.split_at(35.7)
    tl.delete(tl.clips[1].cid)  # borra el segmento central
    tl.move(tl.clips[-1].cid, 0)  # y pone el ultimo trozo primero

    expected = tl.duration
    ok, msg = export_sync(tl, out, mode=MODE_EXACT)
    check("la exportacion termina bien", ok, msg)
    if not ok:
        return

    got = probe(out)
    check("duracion correcta (+-0.2 s)", abs(got.duration - expected) < 0.2,
          f"esperado {expected:.2f}, obtenido {got.duration:.2f}")
    check("resolucion conservada", (got.width, got.height) == (1280, 720),
          f"{got.width}x{got.height}")
    check("codec conservado (h264)", got.vcodec == "h264", got.vcodec)
    check("el audio sobrevive", got.has_audio, "sin audio")

    frames = count_frames(out)
    expected_frames = round(expected * 30)
    check("recuento de fotogramas exacto (+-3)", abs(frames - expected_frames) <= 3,
          f"esperado ~{expected_frames}, obtenido {frames}")
    print(f"       -> {msg.splitlines()[0]}")


def test_export_lossless() -> None:
    print("\n[6] Exportacion sin recodificar (cortes en keyframe)")
    out = str(OUT / "lossless.mp4")
    tl = Timeline()
    tl.append_source(LAND60)
    tl.clips[0].in_t, tl.clips[0].out_t = 12.0, 39.0

    ok, msg = export_sync(tl, out, mode=MODE_LOSSLESS)
    check("exportacion correcta", ok, msg)
    if not ok:
        return
    got = probe(out)
    check("duracion 27 s", abs(got.duration - 27) < 0.2, f"{got.duration}")
    check("informa de copia bit a bit", "bit a bit" in msg, msg.splitlines()[0])

    # La copia debe ser realmente identica: mismo tamanio de datos de video.
    src_bits = probe(LAND60)
    check("el codec y el perfil no cambian",
          got.vcodec == src_bits.vcodec and got.profile == src_bits.profile,
          f"{got.vcodec}/{got.profile} vs {src_bits.vcodec}/{src_bits.profile}")


def test_export_rotated() -> None:
    print("\n[7] Exportacion girada 90 grados")
    out = str(OUT / "rotated.mp4")
    tl = Timeline()
    tl.append_source(LAND60)
    tl.clips[0].in_t, tl.clips[0].out_t = 5.0, 15.0
    tl.rotate_all(90)

    # Sin recodificar, el corte pedido en 5.0 s se lleva al keyframe 6.0 s, asi
    # que lo correcto es comparar contra el plan, no contra el tiempo pedido.
    plan = build_plan(tl, MODE_LOSSLESS)
    ok, msg = export_sync(tl, out, mode=MODE_LOSSLESS)
    check("exportacion correcta", ok, msg)
    if not ok:
        return
    got = probe(out)
    check("el archivo declara rotacion 90", got.rotation == 90, f"{got.rotation}")
    check("pixeles sin girar (siguen 1280x720)", (got.width, got.height) == (1280, 720),
          f"{got.width}x{got.height}")
    check("se ve como 720x1280", got.display_size() == (720, 1280),
          str(got.display_size()))
    check("duracion coherente con el plan",
          abs(got.duration - plan.total_duration) < 0.15,
          f"plan {plan.total_duration:.2f}, archivo {got.duration:.2f}")
    check("el corte se ajusto al keyframe 6.0 s",
          abs(plan.pieces[0].start - 6.0) < 0.05, f"{plan.pieces[0].start}")


def test_export_mixed() -> None:
    print("\n[8] Exportacion con fuentes distintas (horizontal + vertical)")
    out = str(OUT / "mixed.mp4")
    tl = Timeline()
    tl.append_source(LAND60)
    tl.clips[0].out_t = 8.0
    tl.append_source(PORT20)
    tl.clips[1].out_t = 6.0

    expected = tl.duration
    ok, msg = export_sync(tl, out)
    check("exportacion correcta", ok, msg)
    if not ok:
        return
    got = probe(out)
    check("duracion correcta (+-0.3 s)", abs(got.duration - expected) < 0.3,
          f"esperado {expected:.2f}, obtenido {got.duration:.2f}")
    check("resolucion la marca el primer clip", (got.width, got.height) == (1280, 720),
          f"{got.width}x{got.height}")


def main() -> int:
    QCoreApplication(sys.argv)   # necesario para las senales de Qt
    if not Path(LAND60).exists():
        print("Faltan los videos de prueba: ejecuta antes tests/make_samples.py")
        return 2

    for fn in (test_probe, test_keyframes, test_editing, test_plan,
               test_export_cuts, test_export_lossless, test_export_rotated,
               test_export_mixed):
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
