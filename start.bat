@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

rem ===== 人格卡生成工具 一键启动 =====
rem 启动失败时窗口会停留并显示错误信息，也可查看本目录 startup_error.log

set "VENV=C:\Users\bwww\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set "PY=python"

if exist "%VENV%" set "PY=%VENV%"

echo ============================================
echo   人格卡生成工具启动中...
echo   地址: http://127.0.0.1:5000
echo   关闭本窗口即停止服务
echo ============================================

"%PY%" app.py 2>>startup_error.log

if errorlevel 1 (
  echo.
  echo [启动失败] 请查看上方错误信息，或查看 startup_error.log
  echo 常见原因: 端口 5000 被占用（先关闭其它实例）/ 依赖缺失（pip install -r requirements.txt）
  pause
) else (
  echo.
  echo [服务已停止] 按任意键关闭窗口
  pause >nul
)
