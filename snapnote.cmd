@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0snapnote.ps1" %*
set "SNAPNOTE_EXIT=%ERRORLEVEL%"
if not "%SNAPNOTE_EXIT%"=="0" (
  echo.
  echo SnapNote command failed. See the message above.
  if "%~1"=="" pause
)
exit /b %SNAPNOTE_EXIT%
