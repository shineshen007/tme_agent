@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo 请先双击 setup_windows.cmd 配置环境。
  pause
  exit /b 1
)
.venv\Scripts\python.exe native_dist\bootstrap.py start
set "MULTIOMICS_EXIT=%errorlevel%"
if not "%MULTIOMICS_EXIT%"=="0" pause
exit /b %MULTIOMICS_EXIT%
