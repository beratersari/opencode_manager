@echo off
REM =============================================================================
REM OpenCode Session Manager - start BOTH backend (:8080) and frontend (:5173)
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

set "LAUNCH=%HERE%"
if exist "%ROOT%\scripts\start-backend.bat" set "LAUNCH=%ROOT%\scripts"

echo ========================================
echo   OpenCode Session Manager - Start all
echo ========================================
echo.
echo   Backend  : http://127.0.0.1:8080/   ^(API + built SPA^)
echo   Frontend : http://127.0.0.1:5173/   ^(SPA proxy, no Node^)
echo.

if not exist "%LAUNCH%\start-backend.bat" (
    echo [ERROR] start-backend.bat not found.
    call :maybe_pause
    exit /b 1
)
if not exist "%LAUNCH%\start-frontend.bat" (
    echo [ERROR] start-frontend.bat not found.
    call :maybe_pause
    exit /b 1
)

echo === [1/2] Backend ===
set "OSM_NONINTERACTIVE=1"
call "%LAUNCH%\start-backend.bat"
if errorlevel 1 (
    echo [ERROR] Backend failed to start. See "OSM-Backend" window.
    set "OSM_NONINTERACTIVE="
    call :maybe_pause
    exit /b 1
)

echo.
echo === [2/2] Frontend ===
call "%LAUNCH%\start-frontend.bat"
set "RC=%ERRORLEVEL%"
set "OSM_NONINTERACTIVE="

if not "%RC%"=="0" (
    echo [ERROR] Frontend failed. Backend may still be on :8080.
    echo Open http://127.0.0.1:8080/jobs for the manager-hosted UI.
    call :maybe_pause
    exit /b 1
)

echo.
echo ========================================
echo   Both running
echo ========================================
echo Prefer UI:  http://127.0.0.1:5173/
echo Also UI:    http://127.0.0.1:8080/jobs
echo API:        http://127.0.0.1:8080/api/meta
echo.
echo Console windows: OSM-Backend, OSM-Frontend
echo.
call :maybe_pause
exit /b 0

:maybe_pause
if /i "%OSM_NONINTERACTIVE%"=="1" exit /b 0
pause
exit /b 0
