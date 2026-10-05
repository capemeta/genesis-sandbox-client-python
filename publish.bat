@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\release.ps1" %*
set "release_exit=%errorlevel%"
if not "%release_exit%"=="0" echo Release stopped. Read the error above.
exit /b %release_exit%
