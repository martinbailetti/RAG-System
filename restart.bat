@echo off

REM Detener proceso en puerto 8888 (si está corriendo)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8888 2^>nul') do (
    taskkill /PID %%a /F >nul 2>&1
)

echo Proceso detenido. Esperando 2 segundos...
timeout /t 2 /nobreak >nul

REM Iniciar de nuevo
cd /d D:\smidocs

start "" /B D:\smidocs\venv\Scripts\pythonw.exe ^
    -m uvicorn endpoint:app --host 0.0.0.0 --port 8888 ^
    >> D:\smidocs\smidocs.log 2>&1

echo Servidor reiniciado en puerto 8888.
