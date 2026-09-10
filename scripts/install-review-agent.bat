@echo off
REM Copy code-reviewer + skills into %USERPROFILE%\.opencode
REM Does not replace the OpenCode CLI. Use install-opencode.bat for that.

setlocal
set "HERE=%~dp0"
set "HERE=%HERE:~0,-1%"
if exist "%HERE%\opencoderman\agents\code-reviewer.md" (
    set "SRC=%HERE%\opencoderman"
) else if exist "%HERE%\..\opencoderman\agents\code-reviewer.md" (
    set "SRC=%HERE%\..\opencoderman"
) else (
    echo [ERROR] opencoderman\agents\code-reviewer.md is missing.
    goto :end
)

set "DEST=%USERPROFILE%\.opencode"
mkdir "%DEST%\agents" 2>nul
copy /Y "%SRC%\agents\code-reviewer.md" "%DEST%\agents\code-reviewer.md"
copy /Y "%SRC%\agents\code-reviewer.md" "%DEST%\agents\gitlab-reviewer.md"
xcopy /E /I /Y "%SRC%\skills" "%DEST%\skills"
echo [OK] Copied code-reviewer and skills to %DEST%

:end
call :maybe_pause
exit /b 0

:maybe_pause
if /i "%CREASY_NONINTERACTIVE%"=="1" exit /b 0
if /i "%OSM_NONINTERACTIVE%"=="1" exit /b 0
pause
exit /b 0
