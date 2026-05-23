@echo off

REM Buscar PID usando el puerto 8888
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8888') do (
    taskkill /PID %%a /F
)

echo Proceso en puerto 8888 detenido.