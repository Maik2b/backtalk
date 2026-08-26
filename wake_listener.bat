@echo off
rem backtalk: the cold-start "Hey Jarvis" listener — runs standalone,
rem does NOT require the voice line to already be up. See
rem backtalk/wake_listener.py for what it actually does.
cd /d "%~dp0"
uv run python -m backtalk.wake_listener
if errorlevel 1 (
  echo.
  echo   The wake-word listener stopped with an error. The message is above.
  pause
)
