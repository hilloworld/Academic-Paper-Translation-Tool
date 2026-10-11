@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   Paper PDF Translator  /  Bilingual Reader
echo   (paper-pdf-translator)
echo ============================================================
echo.

where uv >nul 2>nul
if errorlevel 1 (
    echo [ERROR] "uv" not found on PATH.
    echo         Install it first: https://docs.astral.sh/uv/
    echo.
    pause
    exit /b 1
)

echo [1/2] Syncing dependencies (uv sync) ...
uv sync
if errorlevel 1 (
    echo.
    echo [ERROR] uv sync failed. Check your network / proxy and try again.
    pause
    exit /b 1
)

echo.
echo [2/2] Starting server. Your browser will open automatically.
echo       Press Ctrl+C in this window to stop.
echo.
uv run python run.py

echo.
echo Server stopped.
pause
