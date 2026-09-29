@echo off
rem Starts the CN-PK Consignment Portal from the source code (needs Python 3.11 or newer).
rem Most people should use the ready-made CNPK-Portal program instead: see docs\05-run-on-your-computer.md
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY goto nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" || goto oldpython

if not exist ".venv\Scripts\python.exe" (
  echo First start: installing what the portal needs. This takes a minute and needs the internet.
  %PY% -m venv .venv || goto failed
)
fc /b launcher\requirements.txt .venv\installed-requirements.txt >nul 2>nul || (
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r launcher\requirements.txt || goto failed
  copy /y launcher\requirements.txt .venv\installed-requirements.txt >nul
)
".venv\Scripts\python.exe" launcher\portal_launcher.py %*
if errorlevel 1 pause
exit /b %errorlevel%

:nopython
echo Python is not installed. Install Python 3.12 from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" - or use the ready-made CNPK-Portal program instead.
pause
exit /b 1

:oldpython
echo The portal needs Python 3.11 or newer. Install it from https://www.python.org/downloads/
pause
exit /b 1

:failed
echo Setting up failed. Check the internet connection and try again.
pause
exit /b 1
