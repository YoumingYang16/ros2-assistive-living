@echo off
setlocal
cd /d "%~dp0"
set "VOICE_PATROL_PYTHON="
if exist "%~dp0.venv\Scripts\python.exe" set "VOICE_PATROL_PYTHON=%~dp0.venv\Scripts\python.exe"
if defined VOICE_PATROL_PYTHON goto run_python
for /f "delims=" %%P in ('where python.exe 2^>nul ^| findstr /v /i "WindowsApps"') do if not defined VOICE_PATROL_PYTHON set "VOICE_PATROL_PYTHON=%%P"
if defined VOICE_PATROL_PYTHON goto run_python
if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" set "VOICE_PATROL_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if defined VOICE_PATROL_PYTHON goto run_python
where py.exe >nul 2>nul
if not errorlevel 1 goto run_launcher
echo Python 3.10+ not found. Install Python or use start_mock.ps1 -PythonPath.
pause
exit /b 1
:run_python
"%VOICE_PATROL_PYTHON%" -m robot_voice_patrol --mode mock %*
goto result
:run_launcher
py -3 -m robot_voice_patrol --mode mock %*
:result
set "VOICE_PATROL_EXIT=%ERRORLEVEL%"
if not "%VOICE_PATROL_EXIT%"=="0" pause
exit /b %VOICE_PATROL_EXIT%
