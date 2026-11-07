@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist "data\splitlink.db" (
  echo [1/3] 首次运行，写入演示数据...
  python scripts\seed_demo.py
) else (
  echo [1/3] 已有数据，跳过初始化
)

echo [2/3] 启动服务 http://127.0.0.1:8787/
start "" cmd /c "timeout /t 3 >nul && start "" http://127.0.0.1:8787/"

echo [3/3] 按 Ctrl+C 停止
echo.
python app.py

pause
