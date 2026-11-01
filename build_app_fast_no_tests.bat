@echo off
setlocal
title Fast Build FM-1 Firmware Tool - No Tests

echo Building FM-1 Firmware Tool without running tests...
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" -SkipTests
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
