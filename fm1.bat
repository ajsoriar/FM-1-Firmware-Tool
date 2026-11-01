@echo off
rem FM-1 Firmware Tool for the console or graphical window.
rem   fm1 install "C:\path\firmware.fwsc"   install from the console (open it as administrator)
rem   fm1 check "C:\path\firmware.fwsc"     inspect package and environment without accessing the FM-1
rem   fm1 detect                            detect the FM-1 (read-only)
rem   fm1 [firmware.fwsc]                   open the window
rem   fm1 install --help                    all options
rem Relative paths are resolved from the current directory.
setlocal
set "PYTHONPATH=%~dp0;%PYTHONPATH%"

if /i "%~1"=="install" goto console
if /i "%~1"=="check" goto console
if /i "%~1"=="detect" goto console
if /i "%~1"=="-h" goto console
if /i "%~1"=="--help" goto console

rem Windowed mode: no console window.
where pyw >nul 2>nul
if %ERRORLEVEL%==0 (start "" pyw -3 -m app %*) else (start "" pythonw -m app %*)
exit /b 0

:console
where py >nul 2>nul
if %ERRORLEVEL%==0 (py -3 -m app %*) else (python -m app %*)
exit /b %ERRORLEVEL%
