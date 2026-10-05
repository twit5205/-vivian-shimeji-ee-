@echo off
chcp 65001 >nul
title XiaoZi Voice Assistant - Doctor
cd /d "%~dp0voice_bridge"

echo ==========================================
echo   XiaoZi Voice Doctor
echo   Diagnose the voice pipeline
echo ==========================================
echo.
echo [1/2] Basic self-check (no network / no mic)...
python -u doctor.py
echo.
echo [2/2] Calibrating microphone (4s)...
echo     Speak 2s, then stay quiet 2s.
python -u doctor.py --mic 4
echo.
echo ------------------------------------------
echo  Optional deep checks:
echo    python doctor.py --net     (Edge-TTS / DeepSeek / Whisper)
echo    python doctor.py --echo    (speaker bleed, barge-in tuning)
echo ------------------------------------------
pause
