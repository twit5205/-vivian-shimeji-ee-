@echo off
title XiaoZi Voice Assistant - Shimeji
cd /d "%~dp0"

echo ==========================================
echo   XiaoZi Voice Assistant starting...
echo   Speak to the mic to chat with the pet.
echo   Close this window to exit voice bridge.
echo.
echo   Mic / TTS problem?  Run doctor_voice.bat
echo ==========================================
echo.

echo [1/2] Starting pet (Shimeji-ee.jar)...
start "XiaoZi Pet" /min javaw -jar "%~dp0Shimeji-ee.jar"
ping 127.0.0.1 -n 4 >nul
ping 127.0.0.1 -n 2 >nul

echo [2/2] Starting voice bridge (auto-starts vva voice if needed)...
cd /d "%~dp0voice_bridge"
python -u voice_assistant.py

echo.
echo Voice bridge exited.
pause
