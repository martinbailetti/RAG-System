@echo off

cd /d D:\docs

start "" /B D:\docs\venv\Scripts\pythonw.exe ^
    -m uvicorn endpoint:app --host 0.0.0.0 --port 8888 ^
    >> D:\docs\docs.log 2>&1