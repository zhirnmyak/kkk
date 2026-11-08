@echo off
rem Keep CRLF line endings: cmd.exe misreads UTF-8 batch files with LF.
chcp 65001 >nul
setlocal
pushd "%~dp0webapp"
where node >nul 2>nul
if errorlevel 1 (
  echo Установите Node.js 20.19 или новее, затем повторите запуск.
  popd
  exit /b 1
)
if not exist node_modules (
  call npm.cmd ci --no-audit --no-fund
  if errorlevel 1 (
    popd
    exit /b 1
  )
)
call npm.cmd run build
set "BUILD_EXIT=%ERRORLEVEL%"
popd
exit /b %BUILD_EXIT%
