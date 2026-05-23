@echo off

cd /d D:\smidocs

start "" /B D:\smidocs\venv\Scripts\pythonw.exe ^
    -m uvicorn endpoint:app --host 0.0.0.0 --port 8888 ^
    >> D:\smidocs\smidocs.log 2>&1