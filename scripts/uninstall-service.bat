@echo off
REM aMIR-mini - remove the Windows service. Elevated prompt.
setlocal
set "HERE=%~dp0"
set "HERE=%HERE:~0,-1%"
if exist "%HERE%\pyproject.toml" (
    set "ROOT=%HERE%"
) else if exist "%HERE%\..\pyproject.toml" (
    for %%I in ("%HERE%\..") do set "ROOT=%%~fI"
) else (
    set "ROOT=%HERE%"
)
cd /d "%ROOT%"
if exist "%ROOT%\.venv\Scripts\python.exe" (
    "%ROOT%\.venv\Scripts\python.exe" -m opencode_manager.service_install uninstall --root "%ROOT%"
    exit /b %ERRORLEVEL%
)
if exist "%ROOT%\service\amir-mini.exe" (
    "%ROOT%\service\amir-mini.exe" stop
    "%ROOT%\service\amir-mini.exe" uninstall
    exit /b %ERRORLEVEL%
)
echo [OK] no service wrapper found
exit /b 0
