@echo off
rem Keep CRLF line endings: cmd.exe misreads UTF-8 batch files with LF.
chcp 65001 >nul
setlocal
rem Локальный показ хакатона: бот + терминал + мини-приложение, без туннеля.
call "%~dp0build-miniapp.cmd"
if errorlevel 1 exit /b 1
set "MAX_LOCAL_MINIAPP=1"
set "HTTP_HOST=127.0.0.1"
call "%~dp0start-terminal.cmd" %*
exit /b %ERRORLEVEL%
