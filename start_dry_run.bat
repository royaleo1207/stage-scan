@echo off
chcp 65001 >nul
rem Double-click to start the order program in dry-run mode (real login, local simulated fills, no real orders)
cd /d "%~dp0"
python -m trader.order_server --dry-run
echo.
echo Program stopped. Press any key to close this window.
pause >nul
