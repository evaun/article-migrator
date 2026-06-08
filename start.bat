@echo off
REM Windows 启动脚本

cd /d "%~dp0"

if not exist "venv" (
    echo 正在创建虚拟环境...
    python -m venv venv
)

call venv\Scripts\activate
pip install -q -r requirements.txt

echo 启动文章搬运助手...
echo 请在浏览器打开: http://localhost:8080
echo 按 Ctrl+C 停止
python app.py
pause
