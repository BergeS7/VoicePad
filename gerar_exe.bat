@echo off
rem Gera o VoicePad.exe (um arquivo só) nesta pasta.
rem Requer: pip install -r requirements.txt pyinstaller
cd /d "%~dp0"
set "AQUI=%~dp0"
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name VoicePad ^
  --icon "%AQUI%assets\voicepad.ico" ^
  --add-data "%AQUI%assets\logo.png;assets" ^
  --add-data "%AQUI%assets\voicepad.ico;assets" ^
  --hidden-import pystray._win32 ^
  --distpath "%AQUI%." --workpath "%AQUI%build" --specpath "%AQUI%build" ^
  "%AQUI%voicepad.py"
