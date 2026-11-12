@echo off
setlocal enabledelayedexpansion

rem ===== 补全基础 PATH，确保 chcp/netstat/findstr/taskkill 在精简/离线系统可用 =====
set "SYS32=%SystemRoot%\System32"
set "PATH=%SYS32%;%SystemRoot%;%SystemRoot%\System32\Wbem;%PATH%"
chcp 65001 >nul

rem ===== 便携根目录（本脚本所在目录）=====
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

rem ===== 内置浏览器与运行时环境变量 =====
set "PLAYWRIGHT_BROWSERS_PATH=%ROOT%\browser"
set "PYTHONHOME="
set "PYTHONPATH="
set "PATH=%ROOT%\runtime;%ROOT%\runtime\Scripts;%PATH%"

rem ===== 工作目录切到 app，确保数据与登录目录存在 =====
cd /d "%ROOT%\app"
if not exist "data" mkdir "data"
if not exist "browser_data" mkdir "browser_data"

rem ===== 端口已在监听则直接打开页面 =====
netstat -ano | findstr ":8765 " | findstr LISTENING >nul 2>nul
if !errorlevel!==0 (
    start "" "http://127.0.0.1:8765"
    goto end
)

rem ===== 后台静默启动 uvicorn 服务（兼容便携 runtime 根目录与标准 venv 两种布局）=====
set "PYEXE=%ROOT%\runtime\python.exe"
if not exist "%PYEXE%" set "PYEXE=%ROOT%\runtime\Scripts\python.exe"
start "资讯采集中心服务" /min "%PYEXE%" -m uvicorn server:app --host 127.0.0.1 --port 8765

rem ===== 轮询等待端口就绪（最多约 30 秒，用 ping 等待以兼容重定向环境）=====
set /a n=0
:wait
ping -n 2 127.0.0.1 >nul
set /a n+=1
netstat -ano | findstr ":8765 " | findstr LISTENING >nul 2>nul
if !errorlevel!==0 goto ready
if !n! geq 30 goto fail
goto wait

:ready
start "" "http://127.0.0.1:8765"
goto end

:fail
echo 服务启动超时，请确认 runtime 目录完整后重试。
pause

:end
endlocal