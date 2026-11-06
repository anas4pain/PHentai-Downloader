@echo off
REM Builds dist\PHentaiDownloader4.exe  (needs Python 3.10+ from python.org on PATH)
REM Only touches this folder - older versions are left alone.
cd /d "%~dp0"
taskkill /im PHentaiDownloader4.exe /f >nul 2>&1
python -m pip install --upgrade -r requirements.txt || goto :err
if exist PHentaiDownloader4.spec del PHentaiDownloader4.spec
python -m PyInstaller --noconfirm --onefile --windowed --name PHentaiDownloader4 --icon icon.ico --add-data "icon.ico;." --collect-all playwright --collect-all curl_cffi --collect-all gallery_dl phentai_downloader.py || goto :err
echo.
echo Done: dist\PHentaiDownloader4.exe
pause
exit /b 0
:err
echo Build failed.
pause
exit /b 1
