@echo off
rem Windows entry point; keep CRLF line endings.
setlocal
where pyw >nul 2>nul
if not errorlevel 1 (
  start "" pyw -3 "%~dp0launcher.pyw"
  exit /b 0
)
where pythonw >nul 2>nul
if not errorlevel 1 (
  start "" pythonw "%~dp0launcher.pyw"
  exit /b 0
)
echo Python 3.14 or newer is required. Install it from https://www.python.org/downloads/windows/
pause
exit /b 1
