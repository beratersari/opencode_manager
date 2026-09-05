@echo off
REM Runs aMIR-mini in the aMIR-mini Backend window. Do not nest this inside a quoted cmd /c.
setlocal EnableDelayedExpansion
cd /d "%~dp0\.."
set "GIT_TERMINAL_PROMPT=0"
set "PYTHONUNBUFFERED=1"
if exist "%USERPROFILE%\.opencode\bin" set "PATH=%USERPROFILE%\.opencode\bin;%PATH%"
if exist "%CD%\vendor\bin" set "PATH=%CD%\vendor\bin;%PATH%"
if not exist "%CD%\logs" mkdir "%CD%\logs"
set "OSM_PY=%CD%\.venv\Scripts\python.exe"
if not exist "%OSM_PY%" (
    echo [ERROR] .venv python missing: %OSM_PY%
    pause
    exit /b 1
)
"%OSM_PY%" -m opencode_manager.app
set "EC=!ERRORLEVEL!"
echo.
echo Backend exited. code=!EC!
>>"%CD%\logs\wrapper-exit.log" echo %DATE% %TIME% exit=!EC!
if not "!EC!"=="0" echo No Python traceback usually means the process was killed from outside.
pause
exit /b !EC!
