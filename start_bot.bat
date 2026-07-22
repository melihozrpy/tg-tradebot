@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo .venv bulunamadi. Once su komutu calistirin:
  echo py -3.11 -m venv .venv
  echo .venv\Scripts\python.exe -m pip install -e ".[dev]"
  exit /b 1
)

".venv\Scripts\python.exe" run_bot.py
