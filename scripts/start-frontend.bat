@echo off
REM =============================================================================
REM OpenCode Session Manager - start FRONTEND only (SPA proxy on :5173)
REM Serves web\dist. Rebuilds it first when local Vite is present.
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

set "FRONTEND_HOST=0.0.0.0"
set "FRONTEND_PORT=5173"
set "BACKEND_URL=http://127.0.0.1:4096"
set "WEB_DIST=%ROOT%\web\dist"
set "VENV_PY=%ROOT%\.venv\Scripts\python.exe"
set "OSM_PY="
if exist "%VENV_PY%" set "OSM_PY=%VENV_PY%"

echo ========================================
echo   OpenCode Session Manager - Frontend
echo ========================================
echo Project  : %ROOT%
echo UI       : http://0.0.0.0:%FRONTEND_PORT%/  ^(open http://127.0.0.1:%FRONTEND_PORT%/ ^)
echo Proxies  : /api and /ws  -^>  %BACKEND_URL%
echo.

if not defined OSM_PY (
    echo [ERROR] .venv is missing.
    echo Run scripts\install.bat first. It creates .venv from vendor\python\windows\python.exe.
    call :maybe_pause
    exit /b 1
)
echo Python   : %OSM_PY%

if exist "%ROOT%\web\node_modules\.bin\vite.cmd" (
    echo Rebuilding web\dist from web\src ...
    pushd "%ROOT%\web"
    call node_modules\.bin\vite.cmd build
    if errorlevel 1 (
        echo [WARNING] vite build failed — serving whatever is in web\dist.
    )
    popd
)

if not exist "%WEB_DIST%\index.html" (
    echo [ERROR] Missing %WEB_DIST%\index.html
    echo Use a CI zip that includes web\dist, or run python packaging\build_dist.py --in-place.
    call :maybe_pause
    exit /b 1
)

echo Checking backend at %BACKEND_URL%/api/meta ...
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri '%BACKEND_URL%/api/meta' -UseBasicParsing -TimeoutSec 5 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Backend is not reachable at %BACKEND_URL%
    echo Start it first:  scripts\start-backend.bat
    call :maybe_pause
    exit /b 1
)
echo [OK] Backend is reachable.

echo Starting SPA proxy in window "OSM-Frontend"...
start "OSM-Frontend" /D "%ROOT%" cmd /c ""%OSM_PY%" -m opencode_manager.dashboard.frontend_proxy --dist "%WEB_DIST%" --backend %BACKEND_URL% --host %FRONTEND_HOST% --port %FRONTEND_PORT% & echo. & echo Frontend exited. & pause"

echo Waiting for UI http://127.0.0.1:%FRONTEND_PORT%/ ...
set /a TRIES=0
:wait_frontend
set /a TRIES+=1
if %TRIES% GTR 30 (
    echo [ERROR] Frontend did not become ready on port %FRONTEND_PORT%.
    echo Open the "OSM-Frontend" window for errors.
    call :maybe_pause
    exit /b 1
)
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'http://127.0.0.1:%FRONTEND_PORT%/' -UseBasicParsing -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if errorlevel 1 (
    timeout /t 2 /nobreak >nul
    goto wait_frontend
)

echo.
echo [OK] Frontend is up.
echo   Open: http://127.0.0.1:%FRONTEND_PORT%/
echo   LAN : http://^<this-pc-ip^>:%FRONTEND_PORT%/
echo   Backend API remains at %BACKEND_URL%
start "" "http://127.0.0.1:%FRONTEND_PORT%/"
echo.
call :maybe_pause
exit /b 0

:maybe_pause
if /i "%OSM_NONINTERACTIVE%"=="1" exit /b 0
pause
exit /b 0
