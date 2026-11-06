@echo off
REM Run from source without building an exe
cd /d "%~dp0"
python -m pip install -q -r requirements.txt
start "" pythonw phentai_downloader.py
