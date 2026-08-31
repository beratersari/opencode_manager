@echo off
REM =============================================================================
REM OpenCode Session Manager - start BACKEND only (API + built SPA on :4096)
REM For a separate UI on :5173, use start-frontend.bat after this.
REM IMPORTANT: never use unescaped "->" in echo lines (cmd redirect).
REM =============================================================================

setlocal EnableDelayedExpansion

set "HERE=%~dp0"
set "HERE=%HERE:~0,-1%"
if exist "%HERE%\pyproject.toml" (
    set "ROOT=%HERE%"
) else if exist "%HERE%\..\pyproject.toml" (
    for %%I in ("%HERE%\..") do set "ROOT=%%~fI"
) else (
    echo [ERROR] Cannot find repo root ^(pyproject.toml^).
    call :maybe_pause
    exit /b 1
)
cd /d "%ROOT%"

set "DASH_PORT=4096"
set "FRONTEND_PORT=5173"
set "VENV_PY=%ROOT%\.venv\Scripts\python.exe"
set "OSM_PY="
if exist "%VENV_PY%" set "OSM_PY=%VENV_PY%"

set "GIT_TERMINAL_PROMPT=0"
set "PYTHONUNBUFFERED=1"
if exist "%USERPROFILE%\.opencode\bin" set "PATH=%USERPROFILE%\.opencode\bin;%PATH%"
if exist "%ROOT%\vendor\bin" set "PATH=%ROOT%\vendor\bin;%PATH%"

echo ========================================
echo   OpenCode Session Manager - Backend
echo ========================================
echo Project : %ROOT%
echo API+SPA : http://0.0.0.0:%DASH_PORT%/  ^(open http://127.0.0.1:%DASH_PORT%/jobs ^)
echo.

if not defined OSM_PY (
    echo [ERROR] .venv is missing.
    echo Run scripts\install.bat first. It creates .venv from vendor\python\windows\python.exe.
    call :maybe_pause
    exit /b 1
)
echo Python  : %OSM_PY%

where opencode >nul 2>&1
if errorlevel 1 (
    echo [WARNING] opencode is not on PATH. Jobs will fail until OpenCode is installed.
    echo           Run scripts\install-opencode.bat ^(wipes old CLI, copies vendor\bin^).
) else (
    echo [OK] opencode on PATH
)

if exist "%ROOT%\web\node_modules\.bin\vite.cmd" (
    echo Rebuilding web\dist from web\src ...
    pushd "%ROOT%\web"
    call node_modules\.bin\vite.cmd build
    if errorlevel 1 (
        echo [WARNING] vite build failed — serving whatever is in web\dist.
    )
    popd
)

if not exist "%ROOT%\web\dist\index.html" (
    echo [WARNING] web\dist\index.html missing — API will run but UI on :%DASH_PORT% will not load.
    echo           Use the CI zip or run python packaging\build_dist.py --in-place.
)

echo Starting manager in window "OSM-Backend"...
start "OSM-Backend" /D "%ROOT%" cmd /c "set PATH=%PATH%&& set GIT_TERMINAL_PROMPT=0&& set PYTHONUNBUFFERED=1&& "%OSM_PY%" -m opencode_manager.app & echo. & echo Backend exited. & pause"

echo Waiting for API http://127.0.0.1:%DASH_PORT%/api/meta ...
set /a TRIES=0
:wait_backend
set /a TRIES+=1
if %TRIES% GTR 45 (
    echo [ERROR] Backend did not become ready on port %DASH_PORT%.
    echo Open the "OSM-Backend" window and read the traceback.
    echo Common issues: missing install, port in use, data_dir permissions.
    call :maybe_pause
    exit /b 1
)
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'http://127.0.0.1:%DASH_PORT%/api/meta' -UseBasicParsing -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if errorlevel 1 (
    timeout /t 2 /nobreak >nul
    goto wait_backend
)

echo.
echo [OK] Backend is up.
echo   API meta : http://127.0.0.1:%DASH_PORT%/api/meta
echo   Dashboard: http://127.0.0.1:%DASH_PORT%/jobs
echo   LAN      : http://^<this-pc-ip^>:%DASH_PORT%/jobs
echo.
echo Optional SPA proxy on port %FRONTEND_PORT%: scripts\start-frontend.bat
echo.
call :maybe_pause
exit /b 0

:maybe_pause
if /i "%OSM_NONINTERACTIVE%"=="1" exit /b 0
pause
exit /b 0
