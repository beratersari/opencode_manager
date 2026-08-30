@echo off
REM =============================================================================
REM OpenCode Session Manager - install manager (offline)
REM Python venv + wheels + prebuilt dashboard. Does NOT install OpenCode.
REM Use install-opencode.bat for the CLI.
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
set "VENV_DIR=%ROOT%\.venv"
set "WHEELS=%ROOT%\vendor\python-wheels"
set "WEB_DIST=%ROOT%\web\dist"
cd /d "%ROOT%"

echo ========================================
echo   OpenCode Session Manager
echo   Install ^(offline^)
echo ========================================
echo.
echo Project : %ROOT%
echo.

if not exist "%WHEELS%" (
    echo [ERROR] vendor\python-wheels is missing.
    echo This installer is offline-only. Use the CI zip, or on a machine with network:
    echo   python packaging\build_dist.py --in-place
    call :maybe_pause
    exit /b 1
)

if not exist "%WEB_DIST%\index.html" (
    echo [ERROR] Missing %WEB_DIST%\index.html
    echo This installer is offline-only and does not run npm.
    echo Use the CI zip, or on a machine with network:
    echo   python packaging\build_dist.py --in-place
    call :maybe_pause
    exit /b 1
)

set "PY="
where python >nul 2>&1
if not errorlevel 1 set "PY=python"
if not defined PY (
    where py >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    echo [ERROR] Python is not installed or not on PATH.
    echo Install a supported 64-bit Python ^(see vendor\SUPPORTED_PYTHON.txt^).
    call :maybe_pause
    exit /b 1
)

for /f "tokens=*" %%a in ('%PY% --version 2^>^&1') do set "PYTHON_VERSION=%%a"
echo [OK] %PYTHON_VERSION%

%PY% -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.11 or newer is required.
    echo Found: %PYTHON_VERSION%
    call :maybe_pause
    exit /b 1
)

if exist "%ROOT%\vendor\SUPPORTED_PYTHON.txt" (
    %PY% -c "import sys,pathlib; p=pathlib.Path(r'%ROOT%')/'vendor'/'SUPPORTED_PYTHON.txt'; lines=[l.strip() for l in p.read_text(encoding='utf-8',errors='ignore').splitlines() if l.strip() and not l.strip().startswith('#')]; ver=f'{sys.version_info.major}.{sys.version_info.minor}'; print('Supported in this package:', ', '.join(lines)); print('Your Python minor:', ver); raise SystemExit(0 if (not lines or ver in lines) else 1)"
    if errorlevel 1 (
        echo [ERROR] Your Python is not in vendor\SUPPORTED_PYTHON.txt.
        call :maybe_pause
        exit /b 1
    )
)

where git >nul 2>&1
if errorlevel 1 (
    echo [WARNING] git is not on PATH. Clone jobs will fail until Git is installed.
) else (
    echo [OK] git found
)

echo.
echo Step 1: Python virtual environment...
if not exist "%VENV_DIR%\Scripts\python.exe" (
    %PY% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        call :maybe_pause
        exit /b 1
    )
    echo [OK] Created %VENV_DIR%
) else (
    echo [OK] Using existing %VENV_DIR%
)

set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

echo.
echo Step 2: Installing packages from vendor\python-wheels ^(no network^)...
"%VENV_PY%" -m pip install --upgrade pip --no-index --find-links="%WHEELS%"
if errorlevel 1 (
    echo [ERROR] Offline pip upgrade failed.
    call :maybe_pause
    exit /b 1
)
"%VENV_PY%" -m pip install --no-index --find-links="%WHEELS%" -e .
if errorlevel 1 (
    echo [ERROR] Offline package install failed.
    echo Check Python version ^(vendor\SUPPORTED_PYTHON.txt^) and 64-bit AMD64.
    call :maybe_pause
    exit /b 1
)
echo [OK] Manager installed into .venv from local wheels

echo.
echo Step 3: Dashboard SPA...
if exist "%WEB_DIST%\assets" (
    echo [OK] Prebuilt dashboard SPA present: web\dist
) else (
    echo [WARNING] web\dist\assets missing — UI may not load
)

echo.
echo ========================================
echo   Manager install complete
echo ========================================
echo.
echo OpenCode is separate:
echo   scripts\install-opencode.bat
echo Then:
echo   scripts\start-backend.bat     API + SPA  http://127.0.0.1:8080/
echo   scripts\start-frontend.bat    SPA proxy  http://127.0.0.1:5173/
echo   scripts\start.bat             both
echo.
call :maybe_pause
exit /b 0

:maybe_pause
if /i "%OSM_NONINTERACTIVE%"=="1" exit /b 0
pause
exit /b 0
