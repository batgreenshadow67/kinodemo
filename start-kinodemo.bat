@echo off
setlocal
cd /d "%~dp0"

REM Find a Python. Prefer pythonw so no console window is shown.
set "PYW="
set "PY="
where pyw >nul 2>nul && set "PYW=pyw"
if not defined PYW ( where pythonw >nul 2>nul && set "PYW=pythonw" )
where py >nul 2>nul && set "PY=py"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY ( where python3 >nul 2>nul && set "PY=python3" )

if not defined PY (
  echo Python was not found on your PATH.
  echo Install Python 3 from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  pause
  exit /b 1
)

REM Node deps are needed for chat logging and browser capture.
if not exist "node_modules" (
  echo Installing Node dependencies for the first time...
  where npm >nul 2>nul
  if errorlevel 1 (
    echo Node.js was not found on your PATH.
    echo Install Node.js from https://nodejs.org/ , then run this file again.
    pause
    exit /b 1
  )
  call npm install
  if errorlevel 1 ( echo npm install failed. & pause & exit /b 1 )
)

if defined PYW (
  REM No console window. Quit from the page when you're done.
  start "" %PYW% kinodemo.py
) else (
  echo No pythonw found; running with a console window instead.
  %PY% kinodemo.py
  if errorlevel 1 pause
)

endlocal