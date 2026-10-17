@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo 请先运行 setup_windows.cmd；诊断不会安装或修复环境。
  pause
  exit /b 1
)
.venv\Scripts\python.exe -B -m native_dist.diagnostics --project-root "%CD%"
set "MULTIOMICS_EXIT=%ERRORLEVEL%"
pause
exit /b %MULTIOMICS_EXIT%
