@echo off
rem Ejecuta las pruebas. La primera vez genera los videos de muestra.
cd /d "%~dp0"
set PY=.venv\Scripts\python.exe
set RC=0

if not exist "tests\samples\landscape_60s.mp4" (
    echo Generando videos de prueba...
    "%PY%" tests\make_samples.py || exit /b 1
)
if not exist "tests\samples\realistic_90s.mp4" (
    echo Generando video con keyframes irregulares...
    "%PY%" tests\make_realistic.py || exit /b 1
)

echo.
echo === Seguridad: validacion de entradas e integridad de bin/ ===
"%PY%" tests\test_security.py
if errorlevel 1 set RC=1

echo.
echo === Nucleo: sondeo, keyframes, edicion y exportacion ===
"%PY%" tests\test_pipeline.py
if errorlevel 1 set RC=1

echo.
echo === Imagen congelada en los cortes (los dos modos) ===
"%PY%" tests\test_freeze.py
if errorlevel 1 set RC=1

echo.
echo === Interfaz: ventana real, mpv y acciones encadenadas ===
"%PY%" tests\test_gui.py
if errorlevel 1 set RC=1

exit /b %RC%
