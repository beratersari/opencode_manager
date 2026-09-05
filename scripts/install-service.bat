@echo off
REM =============================================================================
REM aMIR-mini - install as a Windows service (backend only).
REM Does not change the two-window exe. Run from an elevated prompt.
REM Works next to the shared exe (needs WinSW.exe) or after install.bat (.venv).
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
    set "ROOT=%HERE%"
)
cd /d "%ROOT%"

echo ========================================
echo   aMIR-mini - Install service
echo ========================================
echo Root : %ROOT%
echo.

if exist "%ROOT%\.venv\Scripts\python.exe" (
    "%ROOT%\.venv\Scripts\python.exe" -m opencode_manager.service_install install --root "%ROOT%"
    exit /b %ERRORLEVEL%
)

set "PAYLOAD="
for %%F in ("%ROOT%\amir-mini-*-windows-x64.exe") do set "PAYLOAD=%%~fF"
if not defined PAYLOAD if exist "%ROOT%\amir-mini.exe" set "PAYLOAD=%ROOT%\amir-mini.exe"

set "WINSW="
if exist "%ROOT%\vendor\bin\windows\WinSW.exe" set "WINSW=%ROOT%\vendor\bin\windows\WinSW.exe"
if exist "%ROOT%\WinSW.exe" set "WINSW=%ROOT%\WinSW.exe"
if exist "%ROOT%\WinSW-x64.exe" set "WINSW=%ROOT%\WinSW-x64.exe"

if not defined PAYLOAD (
    echo [ERROR] No aMIR-mini exe in this folder and no .venv.
    echo Put amir-mini-*-windows-x64.exe here, or run install.bat first.
    exit /b 1
)
if not defined WINSW (
    echo [ERROR] WinSW.exe is missing.
    echo Copy WinSW-x64.exe into this folder, or use the Windows zip.
    exit /b 1
)

if not exist "%ROOT%\service\logs" mkdir "%ROOT%\service\logs"
copy /Y "%WINSW%" "%ROOT%\service\amir-mini.exe" >nul
> "%ROOT%\service\amir-mini.xml" (
    echo ^<service^>
    echo   ^<id^>amir-mini^</id^>
    echo   ^<name^>aMIR-mini^</name^>
    echo   ^<description^>aMIR-mini API and dashboard ^(backend only^).^</description^>
    echo   ^<executable^>%PAYLOAD%^</executable^>
    echo   ^<arguments^>--backend-only^</arguments^>
    echo   ^<workingdirectory^>%ROOT%^</workingdirectory^>
    echo   ^<logpath^>%ROOT%\service\logs^</logpath^>
    echo   ^<log mode="roll-by-size"^>
    echo     ^<sizeThreshold^>10240^</sizeThreshold^>
    echo     ^<keepFiles^>8^</keepFiles^>
    echo   ^</log^>
    echo   ^<onfailure action="restart" delay="10 sec"/^>
    echo   ^<env name="GIT_TERMINAL_PROMPT" value="0"/^>
    echo   ^<env name="PYTHONUNBUFFERED" value="1"/^>
    echo ^</service^>
)

echo [OK] payload %PAYLOAD% --backend-only
echo [OK] wrapper %ROOT%\service\amir-mini.exe
"%ROOT%\service\amir-mini.exe" stop >nul 2>&1
"%ROOT%\service\amir-mini.exe" uninstall >nul 2>&1
"%ROOT%\service\amir-mini.exe" install
if errorlevel 1 (
    echo [ERROR] install failed. Run this .bat as Administrator.
    exit /b 1
)
"%ROOT%\service\amir-mini.exe" start
if errorlevel 1 (
    echo [ERROR] installed but did not start. See service\logs and services.msc.
    exit /b 1
)
echo.
echo aMIR-mini is a Windows service. Dashboard: http://127.0.0.1:4096/jobs
echo Two-window exe is unchanged. Stop this service before using that exe on :4096.
echo Git: no GCM popup. Store credentials, or set Log On in services.msc to your user.
exit /b 0
