@echo off
chcp 65001 >nul
rem Double-click to start. Log in, choose mode and accounts in the window that opens.
cd /d "%~dp0"
python -m trader.order_server
echo.
echo Program stopped. Press any key to close this window.
pause >nul
