@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
cd /d "%~dp0"
set "MULTIOMICS_PY="
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -File native_dist\select_python.ps1`) do set "MULTIOMICS_PY=%%P"
if not defined MULTIOMICS_PY (
  echo Compatible Python 3.12–3.14 was not found. Read docs/INSTALL_UPDATE_zh.md.
  pause
  exit /b 1
)
"%MULTIOMICS_PY%" -B native_dist\bootstrap.py check
if errorlevel 1 (
  echo A compatible environment is missing. Follow the Agent installation instructions, then retry.
  pause
  exit /b 1
)
"%MULTIOMICS_PY%" native_dist\bootstrap.py setup %*
set "MULTIOMICS_EXIT=%errorlevel%"
pause
exit /b %MULTIOMICS_EXIT%
