#!/bin/bash
# macOS / Linux 启动脚本

cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
    echo "正在创建虚拟环境..."
    python3 -m venv venv
fi

source venv/bin/activate
pip install -q -r requirements.txt

echo "启动文章搬运助手..."
echo "请在浏览器打开: http://localhost:8080"
echo "按 Ctrl+C 停止"
python app.py
