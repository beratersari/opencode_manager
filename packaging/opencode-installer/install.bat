@echo off
REM =============================================================================
REM aMIR-mini - offline OpenCode 1.18.10 installer (Windows).
REM Wipes %USERPROFILE%\.opencode and copies vendor\bin\windows\opencode.exe.
REM No network. Does not install aMIR-mini itself.
REM IMPORTANT: never use unescaped "->" in echo lines (cmd redirect).
REM =============================================================================

setlocal EnableDelayedExpansion

set "HERE=%~dp0"
set "HERE=%HERE:~0,-1%"
cd /d "%HERE%"

set "PINNED=1.18.10"
set "TARGET=%USERPROFILE%\.opencode"
set "SRC="
if exist "%HERE%\vendor\bin\windows\opencode.exe" set "SRC=%HERE%\vendor\bin\windows\opencode.exe"
if not defined SRC if exist "%HERE%\vendor\bin\opencode.exe" set "SRC=%HERE%\vendor\bin\opencode.exe"

echo ========================================
echo   aMIR-mini OpenCode installer
echo   pinned version %PINNED%
echo ========================================
echo.

if exist "%HERE%\OPENCODE_VERSION.txt" (
    set /p GOT=<"%HERE%\OPENCODE_VERSION.txt"
    set "GOT=!GOT: =!"
    if /I not "!GOT!"=="%PINNED%" (
        echo [ERROR] This zip is not OpenCode %PINNED% ^(found !GOT!^).
        exit /b 1
    )
)

if not defined SRC (
    echo [ERROR] vendor\bin\windows\opencode.exe is missing.
    exit /b 1
)

echo Source : %SRC%
echo Target : %TARGET%
echo.

if exist "%TARGET%" (
    echo Removing previous %TARGET% ...
    rd /s /q "%TARGET%"
    if exist "%TARGET%" (
        echo [ERROR] Could not delete %TARGET%
        exit /b 1
    )
)

mkdir "%TARGET%\bin"
copy /Y "%SRC%" "%TARGET%\bin\opencode.exe" >nul
if not exist "%TARGET%\bin\opencode.exe" (
    echo [ERROR] Copy failed.
    exit /b 1
)

> "%TARGET%\opencode.json" (
    echo {
    echo   "$schema": "https://opencode.ai/config.json",
    echo   "autoupdate": false,
    echo   "plugin": []
    echo }
)

echo [OK] OpenCode %PINNED% installed to %TARGET%\bin\opencode.exe
echo Open a NEW terminal so PATH picks up %%USERPROFILE%%\.opencode\bin
echo.
exit /b 0
