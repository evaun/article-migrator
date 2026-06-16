import os
import re
import io
import time
import zipfile
import base64
from urllib.parse import urlparse
from datetime import datetime

import requests
from bs4 import BeautifulSoup, NavigableString
from flask import Flask, render_template, request, jsonify, send_file
from slugify import slugify

try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload

# ============== 工具函数（从 IGN_Migrator 提取） ==============

def get_valid_filename(s):
    return "".join([c for c in s if c.isalnum() or c in (' ', '-', '_')]).rstrip()

def clean_text_for_hash(text):
    return re.sub(r'\s+|[^\w\u4e00-\u9fa5]', '', text)

def clean_content_tree(soup_element):
    if not soup_element:
        return
    for tag in soup_element.find_all(['script', 'style', 'noscript', 'iframe']):
        tag.decompose()
    garbage_keywords = [
        'avatar', 'login', 'header', 'footer', 'related', 'comment',
        'share', 'author', 'recommend', 'copyright', 'logo', 'friend-link',
        'sidebar', 'advertisement', 'ad-', 'promo', 'social', 'subscribe',
        'newsletter', 'breadcrumb', 'nav', 'menu', 'toolbar'
    ]
    for tag in soup_element.find_all(class_=True):
        classes = tag.get('class')
        if isinstance(classes, list):
            class_str = " ".join(classes).lower()
        else:
            class_str = str(classes).lower()
        for keyword in garbage_keywords:
            if keyword in class_str:
                tag.decompose()
                break

def find_content_smartly(soup, domain):
    if 'ign.com.cn' in domain:
        return soup.find('div', id='id_text')
    if 'ign.com' in domain:
        # IGN US / other regions
        article = soup.find('article') or soup.find('div', class_=lambda x: x and 'article' in x.lower())
        if article:
            return article
        return soup.find('div', id='article-content') or soup.find('div', class_='article-content')
    if 'gouhuo.qq.com' in domain:
        div = soup.find('div', class_='article-content')
        if div:
            return div
        div = soup.find('div', class_='widget-content')
        if div:
            return div
    # Generic density scan
    candidates = []
    for tag in soup.find_all(['article', 'section', 'div']):
        p_count = len(tag.find_all('p', recursive=False))
        if p_count < 2:
            p_count = len(tag.find_all('p', recursive=True)) * 0.5
        if p_count > 3:
            text_len = len(tag.get_text(strip=True))
            candidates.append((p_count, text_len, tag))
    if candidates:
        # Sort by paragraph count first, then text length
        candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return candidates[0][2]
    return None

def fetch_article(url, custom_headers=None):
    domain = urlparse(url).netloc
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if custom_headers:
        headers.update(custom_headers)
    headers['Referer'] = url

    resp = requests.get(url, headers=headers, timeout=20)
    resp.encoding = 'utf-8'
    return resp.text, domain

def extract_article_data(html, url, domain):
    soup = BeautifulSoup(html, 'html.parser')

    # Title
    h1 = soup.find('h1')
    title = h1.get_text().strip() if h1 else (soup.title.get_text().strip() if soup.title else "未命名文章")
    safe_title = get_valid_filename(title)
    if not safe_title:
        safe_title = "article_" + str(int(time.time()))

    # Content
    content_div = find_content_smartly(soup, domain)
    if not content_div:
        # Fallback: try trafilatura if available
        if HAS_TRAFILATURA:
            extracted = trafilatura.extract(html, include_comments=False, include_tables=False)
            if extracted:
                return {
                    'title': title,
                    'safe_title': safe_title,
                    'url': url,
                    'text': extracted,
                    'images': []
                }
        raise ValueError("无法定位正文内容")

    clean_content_tree(content_div)

    stop_phrases = ["本文编译自", "未经授权禁止转载", "相关阅读", "猜你喜欢",
                    "原文链接", "返回搜狐", "您还未登录", "免责声明", "版权声明",
                    "本文由", "原创撰写", "编辑：", "作者：", "撰文："]

    # 用可变状态字典在递归中共享
    state = {
        'text_content': f"标题：{title}\n原文链接：{url}\n\n",
        'img_counter': 1,
        'seen_hashes': set(),
        'seen_img_urls': set(),
        'images_data': [],
        'stop': False,
        'stop_phrases': stop_phrases,
        'url': url,
        'domain': domain,
    }

    # 按文档顺序递归遍历，保证图文位置与原文一致
    walk_nodes(content_div, state)

    return {
        'title': title,
        'safe_title': safe_title,
        'url': url,
        'text': state['text_content'],
        'images': state['images_data']
    }


# 忽略的容器（推荐位、广告、相关阅读卡片等）
SKIP_TAGS = {'aside', 'script', 'style', 'noscript', 'iframe', 'figcaption'}
# 行内元素：文字直接拼进当前段落
INLINE_TAGS = {'span', 'a', 'strong', 'em', 'b', 'i', 'u', 's', 'mark',
               'sub', 'sup', 'small', 'code', 'font', 'label', 'br'}


def walk_nodes(element, state):
    """按文档顺序递归遍历正文节点，原样保留图文相对位置。

    IGN 中国的正文是链式嵌套的 <p>（每个 <p> 内含 直接文字 + figure + 下一个 <p>），
    本函数严格按照 DOM 出现顺序输出：先文字、后图片、再进入下一段，
    从根本上解决图片位置错位的问题。
    """
    para_text = ""  # 累积当前层级的直接文字

    def flush():
        nonlocal para_text
        t = para_text.strip()
        para_text = ""
        if not t:
            return
        # 停止短语检测（短块才触发，避免误伤正文）
        for phrase in state['stop_phrases']:
            if phrase in t and len(t) < 120:
                state['stop'] = True
                return
        if "登录" in t and len(t) < 20:
            return
        # 站内推荐 / 延伸阅读导航行（短块）：丢弃但不触发停止
        nav_prefixes = ("相关文章", "相关阅读", "延伸阅读", "更多内容", "推荐阅读", "还有更多相关内容")
        stripped = t.lstrip("：: ")
        if len(t) < 60 and stripped.startswith(nav_prefixes):
            return
        fp = clean_text_for_hash(t)
        if len(fp) > 5 and fp in state['seen_hashes']:
            return
        if len(fp) > 5:
            state['seen_hashes'].add(fp)
        state['text_content'] += f"{t}\n\n"

    for child in element.children:
        if state['stop']:
            break

        if isinstance(child, NavigableString):
            para_text += str(child)
            continue

        name = getattr(child, 'name', None)
        if not name:
            continue

        if name in SKIP_TAGS:
            continue

        if name in ('figure', 'img'):
            flush()
            if state['stop']:
                break
            emit_image(child, state)
            continue

        if name in ('h2', 'h3', 'h4', 'h5', 'h6'):
            flush()
            if state['stop']:
                break
            htext = child.get_text().strip()
            if "相关阅读" in htext:
                state['stop'] = True
                break
            if htext:
                fp = clean_text_for_hash(htext)
                if not (len(fp) > 5 and fp in state['seen_hashes']):
                    if len(fp) > 5:
                        state['seen_hashes'].add(fp)
                    state['text_content'] += f"【小标题】{htext}\n\n"
            continue

        if name in INLINE_TAGS:
            # 行内元素文字并入当前段落（但内部可能藏 figure，需检查）
            if child.find(['figure', 'img'], recursive=True):
                flush()
                walk_nodes(child, state)
            else:
                para_text += child.get_text()
            continue

        # p / div / blockquote / ul / li 等块级容器：先输出已累积文字，再递归进入
        flush()
        if state['stop']:
            break
        walk_nodes(child, state)

    flush()


def emit_image(element, state):
    """处理单张图片（figure 或 img），写入占位符和图注。"""
    caption = ""
    if element.name == 'figure':
        cap_tag = element.find('figcaption')
        if cap_tag:
            caption = cap_tag.get_text().strip()

    img_obj = element if element.name == 'img' else element.find('img')
    if not img_obj:
        return

    possible_attrs = ['data-src', 'data-original', 'data-url', 'src']
    img_url = None
    for attr in possible_attrs:
        val = img_obj.get(attr)
        if val and not val.startswith('data:'):
            img_url = val
            break

    if not img_url:
        return

    if img_url.startswith('//'):
        img_url = 'https:' + img_url
    elif img_url.startswith('/'):
        img_url = f"https://{state['domain']}" + img_url

    if img_url in state['seen_img_urls']:
        return
    state['seen_img_urls'].add(img_url)

    try:
        img_resp = requests.get(img_url, headers={"User-Agent": "Mozilla/5.0", "Referer": state['url']}, timeout=10)
        ext = 'jpg'
        if 'gif' in img_url.lower():
            ext = 'gif'
        elif 'png' in img_url.lower():
            ext = 'png'
        elif 'webp' in img_url.lower():
            ext = 'webp'

        counter = state['img_counter']
        img_name = f"{counter:02d}.{ext}"
        state['images_data'].append({
            'name': img_name,
            'data': img_resp.content,
            'url': img_url,
            'caption': caption
        })
        state['text_content'] += f"\n【此处插入图片 {counter}】\n"
        if caption:
            cap_fp = clean_text_for_hash(caption)
            if len(cap_fp) > 5 and cap_fp not in state['seen_hashes']:
                state['seen_hashes'].add(cap_fp)
                state['text_content'] += f"【图注】：{caption}\n"
        state['img_counter'] += 1
    except Exception:
        pass

def create_zip_from_article(article_data):
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        # Write text file
        text_filename = "微博文案.txt"
        zf.writestr(text_filename, article_data['text'].encode('utf-8'))
        # Write images
        for img in article_data['images']:
            zf.writestr(img['name'], img['data'])
    zip_buffer.seek(0)
    return zip_buffer

def create_markdown_from_article(article_data):
    md = f"# {article_data['title']}\n\n"
    md += f"> 原文链接: {article_data['url']}\n\n"
    md += "---\n\n"

    lines = article_data['text'].split('\n')
    img_idx = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith('【此处插入图片'):
            img_idx += 1
            md += f"\n![图片{img_idx}]\n\n"
        elif line.startswith('【图注】：'):
            md += f"*{line.replace('【图注】：', '')}*\n\n"
        elif line.startswith('【小标题】'):
            md += f"## {line.replace('【小标题】', '')}\n\n"
        elif line.startswith('标题：') or line.startswith('原文链接：'):
            continue
        else:
            md += f"{line}\n\n"
    return md

# ============== 路由 ==============

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/fetch', methods=['POST'])
def api_fetch():
    data = request.get_json()
    url = data.get('url', '').strip()
    output_format = data.get('format', 'zip')  # zip or markdown

    if not url:
        return jsonify({'success': False, 'error': '请提供文章链接'}), 400

    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url

    try:
        html, domain = fetch_article(url)
        article = extract_article_data(html, url, domain)

        if output_format == 'markdown':
            md_content = create_markdown_from_article(article)
            return jsonify({
                'success': True,
                'title': article['title'],
                'markdown': md_content,
                'image_count': len(article['images'])
            })
        else:
            zip_buffer = create_zip_from_article(article)
            zip_b64 = base64.b64encode(zip_buffer.getvalue()).decode('utf-8')
            return jsonify({
                'success': True,
                'title': article['title'],
                'zip_base64': zip_b64,
                'filename': f"{article['safe_title']}.zip",
                'image_count': len(article['images'])
            })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/batch', methods=['POST'])
def api_batch():
    data = request.get_json()
    urls_text = data.get('urls', '').strip()
    output_format = data.get('format', 'zip')

    if not urls_text:
        return jsonify({'success': False, 'error': '请提供至少一个链接'}), 400

    urls = [u.strip() for u in urls_text.split('\n') if u.strip()]
    results = []
    errors = []

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for idx, url in enumerate(urls):
            if not url.startswith(('http://', 'https://')):
                url = 'https://' + url
            try:
                html, domain = fetch_article(url)
                article = extract_article_data(html, url, domain)

                if output_format == 'markdown':
                    md = create_markdown_from_article(article)
                    folder = f"{idx+1:03d}_{article['safe_title']}"
                    zf.writestr(f"{folder}/article.md", md.encode('utf-8'))
                else:
                    folder = f"{idx+1:03d}_{article['safe_title']}"
                    zf.writestr(f"{folder}/微博文案.txt", article['text'].encode('utf-8'))
                    for img in article['images']:
                        zf.writestr(f"{folder}/{img['name']}", img['data'])

                results.append({'url': url, 'title': article['title'], 'status': 'ok'})
            except Exception as e:
                errors.append({'url': url, 'error': str(e)})
                results.append({'url': url, 'title': None, 'status': 'error'})

    zip_buffer.seek(0)
    zip_b64 = base64.b64encode(zip_buffer.getvalue()).decode('utf-8')

    return jsonify({
        'success': len(errors) < len(urls),
        'zip_base64': zip_b64,
        'filename': f"batch_download_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
        'results': results,
        'errors': errors
    })

@app.route('/api/preview', methods=['POST'])
def api_preview():
    data = request.get_json()
    url = data.get('url', '').strip()

    if not url:
        return jsonify({'success': False, 'error': '请提供文章链接'}), 400

    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url

    try:
        html, domain = fetch_article(url)
        article = extract_article_data(html, url, domain)
        md = create_markdown_from_article(article)

        # Simple HTML preview from markdown-like content
        preview_html = f"<h1>{article['title']}</h1>"
        preview_html += f"<p class='meta'>原文: <a href='{url}' target='_blank'>{url}</a></p>"
        preview_html += "<hr>"

        lines = article['text'].split('\n')
        img_idx = 0
        for line in lines:
            line = line.strip()
            if not line or line.startswith('标题：') or line.startswith('原文链接：'):
                continue
            if line.startswith('【此处插入图片'):
                img_idx += 1
                if img_idx <= len(article['images']):
                    img_b64 = base64.b64encode(article['images'][img_idx-1]['data']).decode('utf-8')
                    preview_html += f"<img src='data:image/jpeg;base64,{img_b64}' class='preview-img' />"
            elif line.startswith('【图注】：'):
                preview_html += f"<p class='caption'>{line.replace('【图注】：', '')}</p>"
            elif line.startswith('【小标题】'):
                preview_html += f"<h2>{line.replace('【小标题】', '')}</h2>"
            else:
                preview_html += f"<p>{line}</p>"

        return jsonify({
            'success': True,
            'title': article['title'],
            'preview_html': preview_html,
            'word_count': len(article['text']),
            'image_count': len(article['images'])
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
