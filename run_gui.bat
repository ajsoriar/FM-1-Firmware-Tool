@echo off
rem Launch FM-1 Firmware Tool. A .fwsc file can be dropped onto this file.
rem Requests administrator privileges on launch (required for ROM mode).
setlocal
cd /d "%~dp0"

where pyw >nul 2>nul
if %ERRORLEVEL%==0 (
    start "" pyw -3 -m app %*
    exit /b 0
)
where pythonw >nul 2>nul
if %ERRORLEVEL%==0 (
    start "" pythonw -m app %*
    exit /b 0
)
echo Python 3.11 or later was not found (pyw / pythonw).
echo Install Python from https://www.python.org/downloads/ and try again.
pause
exit /b 2
