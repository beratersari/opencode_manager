@echo off
REM =============================================================================
REM OpenCode Session Manager - install OpenCode CLI (offline)
REM Detects a previous user install, deletes it, copies vendor\bin from scratch.
REM Does not install Python / the dashboard. Use install.bat for that.
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

echo ========================================
echo   OpenCode Session Manager
echo   OpenCode CLI install ^(offline, from scratch^)
echo ========================================
echo.
echo Project : %ROOT%
echo Target  : %USERPROFILE%\.opencode
echo.

if not exist "%ROOT%\vendor\bin\opencode.exe" (
    if not exist "%ROOT%\vendor\bin\opencode" (
        echo [ERROR] vendor\bin\opencode.exe is missing.
        echo Use the CI zip, or run: python packaging\build_dist.py --in-place
        call :maybe_pause
        exit /b 1
    )
)

set "PY="
if exist "%ROOT%\vendor\python\windows\python.exe" set "PY=%ROOT%\vendor\python\windows\python.exe"
if not defined PY if exist "%ROOT%\.venv\Scripts\python.exe" set "PY=%ROOT%\.venv\Scripts\python.exe"
if not defined PY (
    echo [ERROR] Bundled python.exe missing ^(vendor\python\windows\python.exe^).
    echo Run install.bat from the CI zip, or python packaging\build_dist.py --in-place.
    call :maybe_pause
    exit /b 1
)

echo Python  : %PY%
echo.
"%PY%" "%ROOT%\scripts\install_opencode.py" --root "%ROOT%"
if errorlevel 1 (
    echo [ERROR] OpenCode install failed.
    call :maybe_pause
    exit /b 1
)

echo.
echo Open a NEW terminal so PATH picks up %%USERPROFILE%%\.opencode\bin
echo Then: scripts\start-backend.bat
echo.
call :maybe_pause
exit /b 0

:maybe_pause
if /i "%OSM_NONINTERACTIVE%"=="1" exit /b 0
pause
exit /b 0
