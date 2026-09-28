@echo off
setlocal

title OEE Report Generator

cd /d "%~dp0"

echo ============================================================
echo                 OEE REPORT GENERATOR
echo ============================================================
echo.
echo Starting OEE Report Web GUI...
echo.

REM Start Flask in a separate minimized window.
start "OEE Report Server" /min cmd /c ""%~dp0.venv\Scripts\python.exe" "%~dp0web_gui\app.py""

REM Give Flask a moment to start, then open the GUI.
timeout /t 3 /nobreak >nul
start "" "http://localhost:5000"

echo.
echo OEE Report GUI is running at:
echo http://localhost:5000
echo.
echo Close this launcher window to stop the OEE Report server.
echo.

:WAIT
timeout /t 2 /nobreak >nul

REM Check whether Flask is still listening on port 5000.
netstat -ano | findstr /R /C:":5000 .*LISTENING" >nul
if %errorlevel%==0 goto WAIT

echo.
echo OEE Report server has stopped.
echo Closing launcher...
timeout /t 2 /nobreak >nul
exit /b 0
