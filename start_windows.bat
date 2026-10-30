@echo off
setlocal
cd /d "%~dp0"
title Smart AI Irrigation
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.10+ was not found. Install it from https://www.python.org/downloads/ and tick "Add Python to PATH".
  pause & exit /b 1
)

if not exist .venv (
  echo Creating virtual environment...
  python -m venv .venv || (echo Could not create the virtual environment. & pause & exit /b 1)
)
call .venv\Scripts\activate.bat

echo Installing dependencies...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r backend\requirements.txt || (echo Dependency install failed. & pause & exit /b 1)

if not exist .env (
  copy .env.example .env >nul
  echo.
  echo A new .env file was created. Fill in SUPABASE_SERVICE_KEY, MQTT_PASSWORD and ADMIN_TOKEN,
  echo save the file, close Notepad, and this script will continue.
  notepad .env
)

if /i "%1"=="test" (
  python -m pip install --quiet -r backend\requirements-dev.txt
  python -m pytest backend\tests -q
  pause & exit /b
)
if /i "%1"=="simulate" (
  echo Starting SIMULATED data generator ^(clearly labelled, device id simulator01^)...
  cd backend && python simulator.py
  exit /b
)

echo.
echo Dashboard: http://localhost:5000     Admin: http://localhost:5000/admin.html
echo Press Ctrl+C to stop.
cd backend
python run_server.py
pause
