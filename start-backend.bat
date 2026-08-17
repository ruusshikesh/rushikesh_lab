@echo off
REM Rush Algo - backend startup script (Windows)
REM Double-click this file, or run:  start-backend.bat

cd /d "%~dp0backend"

REM 1. Create the virtual environment the first time only
REM    IMPORTANT: pinned to Python 3.12. pandas 2.2.3 / numpy 1.26.4 have no
REM    prebuilt Windows wheels for 3.13+, so a newer Python makes pip try to
REM    COMPILE them from source and fail without Visual Studio build tools.
if not exist "venv\" (
  echo Creating Python virtual environment ^(Python 3.12^)...
  py -3.12 -m venv venv
  if errorlevel 1 (
    echo.
    echo ERROR: Python 3.12 not found.
    echo Install it from: https://www.python.org/downloads/release/python-31210/
    echo Tick "Add python.exe to PATH" and the "py launcher" during setup.
    pause
    exit /b 1
  )
)

REM 2. Activate it
call venv\Scripts\activate.bat

REM 3. Upgrade pip, then install dependencies
echo Upgrading pip...
python -m pip install --quiet --upgrade pip
echo Installing dependencies...
pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo ERROR: dependency install failed. See the message above.
  echo If a broker SDK is the problem, you can skip it - paper mode does not need it.
  pause
  exit /b 1
)

REM 4. Make sure there's a .env file
if not exist ".env" (
  echo No .env found. Creating one from the template - paper mode by default.
  copy .env.example .env
)

REM 5. Start the server
echo.
echo ================================================================
echo   Rush Algo backend starting on http://localhost:8000
echo   API docs:      http://localhost:8000/docs
echo   Health check:  http://localhost:8000/health
echo   Press Ctrl+C to stop.
echo ================================================================
echo.
uvicorn main:app --reload --port 8000
