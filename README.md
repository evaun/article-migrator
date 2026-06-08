# 文章搬运助手 (Article Migrator)

把原本的 Python 命令行工具改成了 Web 应用，支持在浏览器里直接抓取文章、预览内容、打包下载。

## 功能

- **单篇抓取**：输入文章链接，预览或直接下载（ZIP 打包含图片 / Markdown 纯文本）
- **批量下载**：粘贴多个链接，一键批量抓取打包
- **智能识别**：针对 IGN 中国、篝火营地做了专项适配，其他网站自动通用抓取
- **内容清理**：自动去除广告、导航栏、评论区、相关阅读等干扰元素

## 本地运行

### macOS / Linux

```bash
# 1. 进入项目目录
cd web_migrator

# 2. 创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 启动
python app.py

# 5. 浏览器访问 http://localhost:5000
```

### Windows

```powershell
# 1. 进入项目目录
cd web_migrator

# 2. 创建虚拟环境
python -m venv venv
venv\Scripts\activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 启动
python app.py

# 5. 浏览器访问 http://localhost:5000
```

## 打包成独立可执行文件（Windows）

如果你希望家里 Windows 电脑不需要安装 Python，可以打包成单个 .exe：

```powershell
# 先安装 pyinstaller
pip install pyinstaller

# 打包
pyinstaller --onefile --add-data "templates;templates" --add-data "static;static" app.py

# 输出在 dist/app.exe
```

> 注意：需要在 Windows 环境下执行打包才能生成 Windows 可用的 .exe。可以在 Windows 上按上述步骤装 Python 后执行打包命令。

## 部署到线上（Render 免费版）

1. 注册 [Render](https://render.com/) 账号（支持 GitHub 登录）
2. 新建一个 Web Service
3. 选择你的代码仓库，或直接用本目录
4. 设置：
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app`
   - **Runtime**: Python 3
5. 部署完成后会获得一个 `https://xxx.onrender.com` 的公网地址

## 部署到线上（PythonAnywhere 免费版）

1. 注册 [PythonAnywhere](https://www.pythonanywhere.com/)
2. 上传项目文件到 `~/web_migrator/`
3. 创建一个新的 Web App，选择 Flask + Python 3.x
4. 修改 WSGI 配置文件，将路径指向 `~/web_migrator/app.py`
5. 在 Console 里执行：
   ```bash
   cd ~/web_migrator
   pip install -r requirements.txt
   ```
6. 点击 Reload，获得公网地址

## 文件结构

```
web_migrator/
├── app.py              # Flask 后端（核心抓取逻辑）
├── requirements.txt    # Python 依赖
├── templates/
│   └── index.html      # 前端页面
└── static/             # 静态资源（CSS/JS）
```

## 技术说明

- 后端：Flask + requests + BeautifulSoup4 + trafilatura
- 前端：纯 HTML/CSS/JS，无框架依赖
- 单篇抓取逻辑移植自原 `IGN_Migrator.py`
- 批量下载逻辑移植自原 `final_download.py`
