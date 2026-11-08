@echo off
setlocal

rem Config: GATEWAY_KEY (required), GATEWAY_PORT (8787), GATEWAY_HOST (127.0.0.1),
rem GATEWAY_DEFAULT_MODEL (Qwen3.8-Flash), GATEWAY_MAX_PARALLEL (3), GATEWAY_TIMEOUT_MS (180000)

if "%QODER_EXE%"=="" set "QODER_EXE=%LOCALAPPDATA%\Programs\Qoder\Qoder.exe"

rem При прямом запуске берём ключ, модель и effort из .env приложения.
if "%GATEWAY_KEY%"=="" if exist "%~dp0..\.env" (
  for /f "usebackq tokens=1,* delims==" %%a in ("%~dp0..\.env") do (
    if "%%a"=="LLM_API_KEY" set "GATEWAY_KEY=%%b"
    if "%%a"=="LLM_MODEL" set "GATEWAY_DEFAULT_MODEL=%%b"
    if "%%a"=="LLM_REASONING_EFFORT" set "GATEWAY_REASONING_EFFORT=%%b"
  )
)

if "%GATEWAY_KEY%"=="" (
  echo Set GATEWAY_KEY before starting the gateway.
  exit /b 1
)

where node >nul 2>nul
if %errorlevel%==0 (
  node "%~dp0gateway.mjs"
) else (
  if not exist "%QODER_EXE%" (
    echo Qoder.exe not found at "%QODER_EXE%" and node is not on PATH.
    exit /b 1
  )
  set "ELECTRON_RUN_AS_NODE=1"
  "%QODER_EXE%" "%~dp0gateway.mjs"
)
