@echo off
chcp 65001 >nul
rem Double-click to start the order program in LIVE mode (real orders). You will be asked to type YES.
cd /d "%~dp0"
python -m trader.order_server --live
echo.
echo Program stopped. Press any key to close this window.
pause >nul
