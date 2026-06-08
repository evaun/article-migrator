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

def flatten_nested_p(content_div):
    """展平嵌套的 <p> 结构（IGN 中国特有问题）。

    IGN 中国的 HTML 结构: <p>text+figure+<p>text+figure+<p>...</p></p></p>
    每个 <p> 既包含自己的文字/图片，又嵌套了下一个 <p>。
    这个函数把嵌套结构展平，把每个 <p> 的"直属内容"提升到 content_div 下面，
    消除嵌套关系，使得后续 find_all(recursive=False) 能正常工作。
    """
    for p_tag in content_div.find_all('p'):
        # 检查这个 <p> 里是否有嵌套的子 <p>
        child_ps = p_tag.find_all('p', recursive=False)
        if not child_ps:
            continue
        # 把子 <p> 从当前 <p> 中提取出来，放到当前 <p> 后面
        for child_p in child_ps:
            child_p.extract()
            p_tag.insert_after(child_p)

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

    # 展平嵌套的 <p> 结构，确保后续只遍历直接子节点不会重复
    flatten_nested_p(content_div)

    text_content = f"标题：{title}\n原文链接：{url}\n\n"
    img_counter = 1
    seen_hashes = set()
    seen_img_urls = set()
    images_data = []

    stop_phrases = ["本文编译自", "未经授权禁止转载", "相关阅读", "猜你喜欢",
                    "原文链接", "返回搜狐", "您还未登录", "免责声明", "版权声明",
                    "本文由", "原创撰写", "编辑：", "作者：", "撰文："]

    # 只遍历直接子元素，不再递归 find_all
    tags = content_div.find_all(['p', 'figure', 'img', 'h2', 'h3', 'h4', 'h5', 'div'], recursive=False)
    truncate_mode = False

    for element in tags:
        if truncate_mode:
            break

        # 跳过空 div（可能是残留容器）
        if element.name == 'div' and not element.find(['p', 'img', 'figure', 'h2', 'h3', 'h4', 'h5'], recursive=False):
            # div 里没有我们要的内容元素，跳过
            continue

        # 如果是包含内容的 div，展开其直接子元素处理
        if element.name == 'div' and element.find(['p', 'img', 'figure', 'h2', 'h3', 'h4', 'h5'], recursive=False):
            # 展平 div 内部可能存在的嵌套 p
            flatten_nested_p(element)
            inner_tags = element.find_all(['p', 'figure', 'img', 'h2', 'h3', 'h4', 'h5'], recursive=False)
            for inner in inner_tags:
                result = process_element(inner, stop_phrases, seen_hashes, seen_img_urls,
                                        img_counter, images_data, text_content, url, domain, title)
                if result['truncate']:
                    truncate_mode = True
                    break
                img_counter = result['img_counter']
                text_content = result['text_content']
            continue

        result = process_element(element, stop_phrases, seen_hashes, seen_img_urls,
                                img_counter, images_data, text_content, url, domain, title)
        if result['truncate']:
            truncate_mode = True
            break
        img_counter = result['img_counter']
        text_content = result['text_content']

    return {
        'title': title,
        'safe_title': safe_title,
        'url': url,
        'text': text_content,
        'images': images_data
    }

def process_element(element, stop_phrases, seen_hashes, seen_img_urls,
                    img_counter, images_data, text_content, url, domain, title):
    """处理单个元素，返回更新后的状态。"""
    truncate = False

    # 获取文本 —— 对 <p> 用 get_text() 即可，因为已经展平了嵌套结构
    current_text = element.get_text().strip()

    # Heading-level "相关阅读" trigger
    if element.name in ['h2', 'h3', 'h4', 'h5'] and "相关阅读" in current_text:
        return {'truncate': True, 'img_counter': img_counter, 'text_content': text_content}

    # Stop-phrase detection
    for phrase in stop_phrases:
        if phrase in current_text and len(current_text) < 120:
            truncate = True
            break
    if truncate:
        return {'truncate': True, 'img_counter': img_counter, 'text_content': text_content}

    if "登录" in current_text and len(current_text) < 20:
        return {'truncate': False, 'img_counter': img_counter, 'text_content': text_content}

    # Images
    if element.name in ['figure', 'img']:
        caption = ""
        if element.name == 'figure':
            cap_tag = element.find('figcaption')
            if cap_tag:
                caption = cap_tag.get_text().strip()

        img_obj = element if element.name == 'img' else element.find('img')
        if img_obj:
            possible_attrs = ['data-src', 'data-original', 'data-url', 'src']
            img_url = None
            for attr in possible_attrs:
                val = img_obj.get(attr)
                if val and not val.startswith('data:'):
                    img_url = val
                    break

            if img_url:
                if img_url.startswith('//'):
                    img_url = 'https:' + img_url
                elif img_url.startswith('/'):
                    img_url = f"https://{domain}" + img_url

                if img_url not in seen_img_urls:
                    seen_img_urls.add(img_url)

                    try:
                        img_resp = requests.get(img_url, headers={"User-Agent": "Mozilla/5.0", "Referer": url}, timeout=10)
                        ext = 'jpg'
                        if 'gif' in img_url.lower():
                            ext = 'gif'
                        elif 'png' in img_url.lower():
                            ext = 'png'
                        elif 'webp' in img_url.lower():
                            ext = 'webp'

                        img_name = f"{img_counter:02d}.{ext}"
                        images_data.append({
                            'name': img_name,
                            'data': img_resp.content,
                            'url': img_url,
                            'caption': caption
                        })
                        text_content += f"\n【此处插入图片 {img_counter}】\n"
                        if caption:
                            cap_fp = clean_text_for_hash(caption)
                            if len(cap_fp) > 5 and cap_fp not in seen_hashes:
                                seen_hashes.add(cap_fp)
                                text_content += f"【图注】：{caption}\n"
                        img_counter += 1
                    except Exception:
                        pass
        return {'truncate': False, 'img_counter': img_counter, 'text_content': text_content}

    # 先处理 <p> 内部嵌套的 figure/img（展平后 figure 仍在 <p> 内部）
    if element.name == 'p':
        inner_media = element.find_all(['figure', 'img'], recursive=False)
        for media_el in inner_media:
            result = process_element(media_el, stop_phrases, seen_hashes, seen_img_urls,
                                    img_counter, images_data, text_content, url, domain, title)
            if result['truncate']:
                return result
            img_counter = result['img_counter']
            text_content = result['text_content']

    # Text
    if element.name in ['p', 'h2', 'h3', 'h4', 'h5']:
        # 对 <p> 只取直接 NavigableString，排除已处理的 figure/img 文字
        if element.name == 'p':
            from bs4 import NavigableString
            text = ''.join(str(s) for s in element.contents
                          if isinstance(s, NavigableString)).strip()
        else:
            text = current_text
        if not text:
            return {'truncate': False, 'img_counter': img_counter, 'text_content': text_content}
        text_fingerprint = clean_text_for_hash(text)
        if len(text_fingerprint) > 5 and text_fingerprint in seen_hashes:
            return {'truncate': False, 'img_counter': img_counter, 'text_content': text_content}
        seen_hashes.add(text_fingerprint)

        if element.name.startswith('h'):
            text_content += f"【小标题】{text}\n\n"
        else:
            text_content += f"{text}\n\n"

    return {'truncate': False, 'img_counter': img_counter, 'text_content': text_content}

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
