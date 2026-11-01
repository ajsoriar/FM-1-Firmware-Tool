@echo off
setlocal
title Build FM-1 Firmware Tool

echo Building FM-1 Firmware Tool...
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1"
set "BUILD_EXIT=%ERRORLEVEL%"

echo.
if "%BUILD_EXIT%"=="0" (
    echo Build completed successfully.
    echo See the build output above for the versioned application and ZIP paths.
    echo Output folder: "%~dp0dist"
) else (
    echo Build failed. Review the error message above.
)

echo.
pause
exit /b %BUILD_EXIT%
