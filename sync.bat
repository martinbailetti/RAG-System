@echo off
REM filepath: c:\Projects\rag\sync.bat
echo ===================================
echo   Sincronizacion diaria de manuales
echo ===================================
echo.

REM Forzar UTF-8 en la salida de Python para evitar errores charmap con tildes
set PYTHONIOENCODING=utf-8

REM Activar entorno virtual si existe
if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
) else if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
)

REM Ejecutar sincronización y esperar a que termine
echo [%date% %time%] Iniciando sincronizacion...
python sync_ingesta.py
set SYNC_EXIT_CODE=%errorlevel%

if %SYNC_EXIT_CODE% neq 0 (
    echo.
    echo [ERROR] La sincronizacion fallo con codigo %SYNC_EXIT_CODE%
    echo No se reiniciara el servidor.
    pause
    exit /b %SYNC_EXIT_CODE%
)

echo.
echo [%date% %time%] Sincronizacion completada exitosamente
echo.

REM Reiniciar servidor FastAPI
echo Reiniciando servidor...
call stop.bat
timeout /t 3 /nobreak >nul
call start.bat

echo.
echo [%date% %time%] Servidor reiniciado
echo Proceso completo.