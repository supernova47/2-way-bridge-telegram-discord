@echo off
setlocal
cd /d "%~dp0"

if not exist "%~dp0scripts\start.ps1" (
    echo Error: scripts\start.ps1 was not found next to this file.
    pause
    exit /b 1
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" -RestartOnFailure
set "BRIDGE_EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%BRIDGE_EXIT_CODE%"=="0" echo Bridge exited with code %BRIDGE_EXIT_CODE%.
pause
exit /b %BRIDGE_EXIT_CODE%
