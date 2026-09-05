@echo off
REM =============================================================================
REM aMIR-mini - install manager (offline)
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
echo   aMIR-mini
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

set "BUNDLED_PY=%ROOT%\vendor\python\windows\python.exe"
if not exist "%BUNDLED_PY%" (
    echo [ERROR] Missing %BUNDLED_PY%
    echo The zip must include a bundled python.exe. Use the CI zip, or:
    echo   python packaging\build_dist.py --in-place
    call :maybe_pause
    exit /b 1
)
for /f "tokens=*" %%a in ('"%BUNDLED_PY%" --version 2^>^&1') do set "PYTHON_VERSION=%%a"
echo [OK] Bundled %PYTHON_VERSION%
echo      %BUNDLED_PY%

where git >nul 2>&1
if errorlevel 1 (
    echo [WARNING] git is not on PATH. Clone jobs will fail until Git is installed.
) else (
    echo [OK] git found
)

echo.
echo Step 1: Python virtual environment from bundled python.exe...
if exist "%VENV_DIR%" (
    echo Removing existing .venv so it matches the bundled interpreter...
    rmdir /s /q "%VENV_DIR%"
    if exist "%VENV_DIR%" (
        echo [ERROR] Could not remove %VENV_DIR%
        call :maybe_pause
        exit /b 1
    )
)
"%BUNDLED_PY%" -m venv "%VENV_DIR%"
if errorlevel 1 (
    echo [ERROR] Failed to create virtual environment with bundled python.exe.
    call :maybe_pause
    exit /b 1
)
echo [OK] Created %VENV_DIR%

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
    echo Wheels must match the bundled python.exe in vendor\python\windows.
    echo Need a Windows PyYAML wheel ^(PyYAML-*-win_amd64.whl^) in vendor\python-wheels.
    echo Present yaml / pydantic-core wheels:
    dir /b "%WHEELS%\*yaml*" 2>nul
    dir /b "%WHEELS%\*pydantic_core*" 2>nul
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
echo Step 4: settings.local.yaml...
if exist "%ROOT%\settings.local.yaml" (
    echo [OK] settings.local.yaml present
) else if exist "%ROOT%\settings.local.windows.yaml" (
    copy /Y "%ROOT%\settings.local.windows.yaml" "%ROOT%\settings.local.yaml" >nul
    echo [OK] settings.local.yaml from settings.local.windows.yaml ^(data_dir C:\osm^)
) else (
    echo [OK] no overlay; Windows default data_dir is C:\osm
)

echo.
echo ========================================
echo   Manager install complete
echo ========================================
echo.
echo OpenCode is separate:
echo   scripts\install-opencode.bat
echo Then:
echo   scripts\start-backend.bat     API + SPA  http://127.0.0.1:4096/
echo   scripts\start-frontend.bat    SPA proxy  http://127.0.0.1:5173/
echo   scripts\start.bat             both
echo   scripts\install-service.bat   Windows service ^(backend only^)
echo.
call :maybe_pause
exit /b 0

:maybe_pause
if /i "%OSM_NONINTERACTIVE%"=="1" exit /b 0
pause
exit /b 0
