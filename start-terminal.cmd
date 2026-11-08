@echo off
chcp 65001 >nul
setlocal EnableExtensions

rem ============================================================
rem  start-terminal.cmd — МаксИИмка в одном окне: консоль + MAX-бот.
rem  Токен бота лежит в max_token.txt рядом с этим файлом (в git не попадает).
rem    start-terminal.cmd                     — модель и шлюз из /настройки
rem    start-terminal.cmd qwen                — разово Qwen3.8-Flash
rem    start-terminal.cmd deepseek            — разово DeepSeek-Flash
rem    start-terminal.cmd deepseek файл.txt   — прогон сценария без клавиатуры
rem  Шлюз запускается скрыто самой МаксИИмкой и гасится при выходе.
rem ============================================================

set "ROOT=%~dp0"
set "APP=%ROOT:~0,-1%"

if not exist "%APP%\.env" (
  echo [ERR] Не найден "%APP%\.env"
  exit /b 1
)
if not exist "%APP%\.venv\Scripts\python.exe" (
  echo [ERR] Нет "%APP%\.venv". Восстановить:
  echo        py -3 -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
  exit /b 1
)

set "MODELARG="
set "SESSION="
if /i "%~1"=="qwen"     set "MODELARG=Qwen3.8-Flash"
if /i "%~1"=="deepseek" set "MODELARG=DeepSeek-Flash"
if /i "%~1"=="ds"       set "MODELARG=DeepSeek-Flash"
if defined MODELARG (set "SESSION=%~2") else (set "SESSION=%~1")
if defined SESSION set "SESSION=%SESSION:/=\%"
if defined MODELARG (
  set "LLM_MODEL=%MODELARG%"
  set "MAXIMKA_MODEL_EXPLICIT=1"
  set "MAXIMKA_QODER_EXPLICIT=1"
) else (
  rem без аргумента консоль возьмёт модель из /настройки
  set "LLM_MODEL="
  set "MAXIMKA_MODEL_EXPLICIT="
  set "MAXIMKA_QODER_EXPLICIT="
)

pushd "%APP%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if defined SESSION (
  if not exist "%SESSION%" (
    echo [ERR] Файл сценария не найден: %SESSION%
    popd
    exit /b 1
  )
  type "%SESSION%" | ".venv\Scripts\python.exe" debug_console.py
) else (
  ".venv\Scripts\python.exe" debug_console.py
)
set "PYEXIT=%ERRORLEVEL%"
popd
exit /b %PYEXIT%
