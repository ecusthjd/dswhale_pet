@echo off
rem Big Fat Fish Eats Rice -- double-click to launch (pythonw: no console window).
rem Want a console to see errors? Run it from a terminal instead: python dswhale_pet.py
setlocal
cd /d "%~dp0"

where pythonw.exe >nul 2>nul
if errorlevel 1 goto console

start "" pythonw.exe "%~dp0dswhale_pet.py" %*
goto :eof

:console
echo [note] pythonw.exe not found; running with python in the foreground (closing this window closes the pet).
echo        Missing dependency? python -m pip install PyQt5
python "%~dp0dswhale_pet.py" %*
