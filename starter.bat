@echo off
setlocal EnableExtensions

rem Re-launch through Windows UAC before any local service or scan process starts.
fltmc >nul 2>&1
if errorlevel 1 (
  echo Requesting administrator approval for Deerstalker...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs" >nul 2>&1
  exit /b
)

rem Deerstalker local launcher for the configured Ollama model.
cd /d "%~dp0"

rem ---- Fallback defaults, only used for keys missing from .env ----
set "OLLAMA_HOST=http://127.0.0.1:11434"
set "OLLAMA_MODEL=gemma3:12b-it-qat"
set "OLLAMA_FAST_MODEL="
set "OLLAMA_DETAILED_MODEL="
set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"

rem ---- Load every KEY=VALUE line from .env, overriding the defaults above ----
if not exist ".env" (
  echo No .env file found next to this script.
  if exist "_env" (
    echo Creating .env from the _env template...
    copy /y "_env" ".env" >nul
    echo Edit .env for this computer ^(model names, PORTABLE_STAGE_DIR, etc.^), then run this file again.
  ) else (
    echo Create a .env file with your Deerstalker settings, then run this file again.
  )
  pause
  exit /b 1
)

for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
  if not "%%A"=="" set "%%A=%%B"
)

echo.
echo  Deerstalker - local file investigation
echo  Model: %OLLAMA_MODEL%
echo.

if not exist "%OLLAMA_EXE%" (
  echo Ollama was not found at:
  echo   %OLLAMA_EXE%
  echo Install Ollama from https://ollama.com, then run this file again.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  where py >nul 2>&1
  if errorlevel 1 (
    echo Python 3.11 or newer is required. Install Python, then run this file again.
    pause
    exit /b 1
  )
  echo Creating the local Python environment...
  py -3 -m venv .venv
  if errorlevel 1 (
    echo Could not create .venv.
    pause
    exit /b 1
  )
)

echo Checking Deerstalker dependencies...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo Could not install the Python dependencies.
  pause
  exit /b 1
)

call :ollama_ready
if errorlevel 1 (
  echo Starting Ollama locally...
  powershell -NoProfile -Command "Start-Process -FilePath $env:ComSpec -ArgumentList '/c','ollama serve' -WindowStyle Hidden" >nul 2>&1
  if errorlevel 1 (
    echo Could not start Ollama.
    pause
    exit /b 1
  )
)

echo Waiting for Ollama...
set "OLLAMA_READY="
for /l %%I in (1,1,60) do (
  call :ollama_ready
  if not errorlevel 1 set "OLLAMA_READY=1"
  if defined OLLAMA_READY goto :ollama_available
  timeout /t 1 /nobreak >nul
)
echo Ollama did not become ready on %OLLAMA_HOST%.
pause
exit /b 1

:ollama_available
"%OLLAMA_EXE%" list | findstr /i /c:"%OLLAMA_MODEL%" >nul
if errorlevel 1 (
  echo The required model is not installed: %OLLAMA_MODEL%
  echo Run this command once, then start Deerstalker again:
  echo   "%OLLAMA_EXE%" pull %OLLAMA_MODEL%
  pause
  exit /b 1
)

echo Opening Deerstalker...
".venv\Scripts\python.exe" main.py
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
  echo Deerstalker closed with exit code %EXIT_CODE%.
  pause
)
exit /b %EXIT_CODE%

:ollama_ready
curl.exe -fsS --max-time 2 "%OLLAMA_HOST%/api/tags" >nul 2>&1
exit /b %ERRORLEVEL%
