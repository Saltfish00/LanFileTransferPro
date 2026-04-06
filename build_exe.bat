@echo off
chcp 65001 >nul
cd /d %~dp0

echo [1/3] Installing dependencies...
pip install -r requirements.txt
if errorlevel 1 goto :error

echo [2/3] Building exe with spec...
pyinstaller LanFileTransferPro.spec --clean
if errorlevel 1 goto :error

echo [3/3] Done.
echo Output: dist\LanFileTransferPro.exe
pause
exit /b 0

:error
echo Build failed.
pause
exit /b 1
