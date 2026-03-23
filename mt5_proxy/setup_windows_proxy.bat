@echo off
REM ============================================================
REM setup_windows_proxy.bat
REM ============================================================
REM Windows VPS Setup Script สำหรับ MT5 Proxy Server
REM
REM วิธีใช้:
REM   1. RDP เข้า Windows VPS
REM   2. คัดลอกไฟล์ทั้งหมดไปไว้ใน C:\mt5proxy\
REM   3. Double-click setup_windows_proxy.bat
REM   4. แก้ไข .env ให้ตรง
REM   5. รัน start_proxy.bat
REM
REM Prerequisites:
REM   - Windows 10/11 หรือ Windows Server 2019+
REM   - Python 3.10+ (64-bit)
REM   - MetaTrader 5 Terminal ติดตั้งแล้ว + login FTMO account
REM ============================================================

echo.
echo ========================================
echo   MT5 Proxy Server — Windows VPS Setup
echo ========================================
echo.

REM ── Check Python
python --version 2>NUL
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python not found!
    echo         Download from https://www.python.org/downloads/
    echo         Make sure to check "Add Python to PATH" during install
    pause
    exit /b 1
)
echo [OK] Python found

REM ── Create project directory
if not exist "C:\mt5proxy" mkdir "C:\mt5proxy"
cd /d "C:\mt5proxy"

REM ── Install dependencies
echo.
echo Installing Python packages...
pip install fastapi uvicorn MetaTrader5 python-dotenv --upgrade
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] pip install failed
    pause
    exit /b 1
)
echo [OK] Dependencies installed

REM ── Create .env if not exists
if not exist ".env" (
    echo.
    echo Creating .env from template...
    echo # MT5 Proxy Server Configuration > .env
    echo MT5_PROXY_API_KEY=CHANGE-ME >> .env
    echo MT5_PROXY_HMAC_SECRET=CHANGE-ME >> .env
    echo MT5_LOGIN=0 >> .env
    echo MT5_PASSWORD= >> .env
    echo MT5_SERVER=FTMO-Server >> .env
    echo MT5_PROXY_HOST=0.0.0.0 >> .env
    echo MT5_PROXY_PORT=8500 >> .env
    echo MT5_PROXY_ALLOWED_IPS=* >> .env
    echo MT5_PROXY_RATE_LIMIT=30 >> .env
    echo.
    echo [IMPORTANT] Edit C:\mt5proxy\.env with your actual credentials!
)

REM ── Create startup script
echo.
echo Creating start_proxy.bat...
(
    echo @echo off
    echo cd /d C:\mt5proxy
    echo echo Starting MT5 Proxy Server...
    echo echo Press Ctrl+C to stop
    echo echo.
    echo python mt5_proxy_server.py
    echo pause
) > start_proxy.bat

REM ── Create Windows Task Scheduler XML (auto-start on boot)
echo.
echo Creating auto-start task...
(
    echo ^<?xml version="1.0" encoding="UTF-16"?^>
    echo ^<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"^>
    echo   ^<RegistrationInfo^>
    echo     ^<Description^>MT5 Proxy Server - FTMO Bridge^</Description^>
    echo   ^</RegistrationInfo^>
    echo   ^<Triggers^>
    echo     ^<LogonTrigger^>
    echo       ^<Enabled^>true^</Enabled^>
    echo       ^<Delay^>PT30S^</Delay^>
    echo     ^</LogonTrigger^>
    echo   ^</Triggers^>
    echo   ^<Principals^>
    echo     ^<Principal^>
    echo       ^<LogonType^>InteractiveToken^</LogonType^>
    echo       ^<RunLevel^>HighestAvailable^</RunLevel^>
    echo     ^</Principal^>
    echo   ^</Principals^>
    echo   ^<Settings^>
    echo     ^<MultipleInstancesPolicy^>IgnoreNew^</MultipleInstancesPolicy^>
    echo     ^<DisallowStartIfOnBatteries^>false^</DisallowStartIfOnBatteries^>
    echo     ^<StopIfGoingOnBatteries^>false^</StopIfGoingOnBatteries^>
    echo     ^<ExecutionTimeLimit^>PT0S^</ExecutionTimeLimit^>
    echo     ^<RestartOnFailure^>
    echo       ^<Interval^>PT1M^</Interval^>
    echo       ^<Count^>999^</Count^>
    echo     ^</RestartOnFailure^>
    echo   ^</Settings^>
    echo   ^<Actions^>
    echo     ^<Exec^>
    echo       ^<Command^>python^</Command^>
    echo       ^<Arguments^>mt5_proxy_server.py^</Arguments^>
    echo       ^<WorkingDirectory^>C:\mt5proxy^</WorkingDirectory^>
    echo     ^</Exec^>
    echo   ^</Actions^>
    echo ^</Task^>
) > mt5proxy_task.xml

REM Register task (requires admin)
schtasks /create /tn "MT5ProxyServer" /xml mt5proxy_task.xml /f 2>NUL
if %ERRORLEVEL% EQU 0 (
    echo [OK] Auto-start task created
) else (
    echo [WARN] Could not create auto-start task. Run as Administrator if needed.
)

REM ── Firewall rule
echo.
echo Opening port 8500 in Windows Firewall...
netsh advfirewall firewall add rule name="MT5 Proxy Server" dir=in action=allow protocol=TCP localport=8500 2>NUL
if %ERRORLEVEL% EQU 0 (
    echo [OK] Firewall rule added
) else (
    echo [WARN] Could not add firewall rule. Run as Administrator if needed.
)

echo.
echo ========================================
echo   Setup Complete!
echo ========================================
echo.
echo Next steps:
echo   1. Edit C:\mt5proxy\.env with your MT5 credentials
echo   2. Make sure MT5 Terminal is running and logged in
echo   3. Run: start_proxy.bat
echo   4. Test: curl http://localhost:8500/health
echo.
echo For SSL (recommended for production):
echo   - Use Caddy or nginx reverse proxy
echo   - Or generate self-signed cert:
echo     openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes
echo.
pause
