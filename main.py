import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
import yt_dlp
import requests
import re
import urllib.parse

import os
import shutil
from fastapi.responses import FileResponse
from fastapi.background import BackgroundTasks

# 确保 bin 目录在 PATH 中，以便 yt-dlp 找到 ffmpeg
os.environ["PATH"] += os.pathsep + os.path.abspath("bin")

app = FastAPI()

# 挂载静态文件和模板
# Vercel Serverless 环境源码目录通常是只读的，无法动态创建目录
# 我们已在 git 中添加了 static/.gitkeep 以确保目录存在
# 但为了双重保险，如果目录仍然不存在（极少数情况），我们跳过挂载，避免 500 错误
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")
else:
    print("Warning: 'static' directory not found. Static files will not be served.")

templates = Jinja2Templates(directory="templates")

# 临时下载目录
# 在 Vercel 等无服务器环境中，只有 /tmp 是可写的
DOWNLOAD_DIR = "/tmp/downloads" if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME") else "downloads"

if not os.path.exists(DOWNLOAD_DIR):
    try:
        os.makedirs(DOWNLOAD_DIR)
    except Exception as e:
        print(f"Warning: Could not create download dir {DOWNLOAD_DIR}: {e}")
        # Fallback to /tmp directly if subfolder creation fails
        DOWNLOAD_DIR = "/tmp"

def cleanup_file(path: str):
    """后台任务：清理临时文件"""
    try:
        if os.path.exists(path):
            os.remove(path)
            print(f"Deleted temp file: {path}")
    except Exception as e:
        print(f"Error deleting file {path}: {e}")

class VideoRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    title: str = "video"

def get_kg_video_info(url):
    """
    全民K歌手动解析逻辑
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    try:
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        content = resp.text
        
        video_url = None
        title = "全民K歌视频"
        thumbnail = ""
        
        # 1. 尝试从 window.__DATA__ 中提取
        data_match = re.search(r'window\.__DATA__\s*=\s*({.*?});', content, re.DOTALL)
        if data_match:
            try:
                # 简单解析 JSON 字符串
                json_str = data_match.group(1)
                
                # 提取 playurl
                playurl_match = re.search(r'"playurl":"(.*?)"', json_str)
                if playurl_match:
                    video_url = playurl_match.group(1)
                
                # 提取标题 (content 或 nick)
                nick_match = re.search(r'"nick":"(.*?)"', json_str)
                content_match = re.search(r'"content":"(.*?)"', json_str)
                
                if content_match and nick_match:
                     title = f"{nick_match.group(1)} - {content_match.group(1)}"
                elif content_match:
                     title = content_match.group(1)
                elif nick_match:
                     title = nick_match.group(1)
                
                # 提取封面
                cover_match = re.search(r'"cover":"(.*?)"', json_str)
                if cover_match:
                    thumbnail = cover_match.group(1)
                    
            except Exception as e:
                print(f"JSON Parse Error: {e}")

        # 2. 如果 JSON 提取失败，回退到旧的正则匹配
        if not video_url:
            # 尝试提取播放地址
            # 全民K歌页面通常包含 playurl 变量
            play_url_match = re.search(r'playurl\s*[:=]\s*["\'](.*?)["\']', content)
            if not play_url_match:
                play_url_match = re.search(r'src\s*=\s*["\'](http.*?mp4.*?)["\']', content)
            
            if play_url_match:
                video_url = play_url_match.group(1)
                
            # 提取标题
            title_match = re.search(r'<title>(.*?)</title>', content)
            if title_match:
                 title = title_match.group(1)
        
        if video_url:
            # 判断文件类型 (m4a 是音频)
            ext = "mp4"
            type_label = "video"
            if ".m4a" in video_url or ".mp3" in video_url:
                ext = "m4a"
                type_label = "audio"

            return {
                "title": title,
                "thumbnail": thumbnail,
                "formats": [{
                    "url": video_url,
                    "ext": ext,
                    "format_note": "Default",
                    "filesize": 0, # 未知大小
                    "has_audio": True,
                    "type": type_label
                }]
            }
        return None
    except Exception as e:
        print(f"KG Parse Error: {e}")
        return None

def get_bilibili_video_info_fallback(url):
    """
    Bilibili API 备用解析逻辑 (当 yt-dlp 412 时使用)
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/"
    }
    
    try:
        # 1. 提取 BV 号
        bvid_match = re.search(r'(BV\w+)', url)
        if not bvid_match:
            return None
        bvid = bvid_match.group(1)
        
        # 2. 获取视频信息 (CID, 标题)
        info_api = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        resp = requests.get(info_api, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        
        if data['code'] != 0:
            print(f"Bili API Error: {data['message']}")
            return None
            
        video_data = data['data']
        title = video_data['title']
        thumbnail = video_data['pic']
        duration = video_data['duration']
        cid = video_data['cid']
        
        # 3. 获取播放地址
        # platform=html5 通常返回 mp4，不需要复杂的 dash 合并
        play_api = f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn=64&type=mp4&platform=html5&high_quality=1"
        play_resp = requests.get(play_api, headers=headers)
        play_data = play_resp.json()
        
        formats = []
        if play_data['code'] == 0 and 'durl' in play_data['data']:
            for durl in play_data['data']['durl']:
                formats.append({
                    "url": durl['url'],
                    "ext": "mp4",
                    "format_note": "API Fallback (可能会有画质限制)",
                    "filesize": durl.get('size'),
                    "format_id": "api_mp4",
                    "has_audio": True,
                    "type": "video"
                })
        
        return {
            "title": title,
            "thumbnail": thumbnail,
            "duration": duration,
            "webpage_url": f"https://www.bilibili.com/video/{bvid}",
            "formats": formats
        }
        
    except Exception as e:
        print(f"Bili Fallback Error: {e}")
        return None

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/api/resolve")
async def resolve_video(request: VideoRequest):
    url = request.url
    
    # 1. 优先尝试自定义轻量级解析 (针对 Bilibili 和 KG)
    # 这样可以避免 invoke yt-dlp (较慢且易被屏蔽)
    try:
        if "kg.qq.com" in url:
            info = get_kg_video_info(url)
            if info:
                return info
                
        if "bilibili.com" in url or "b23.tv" in url:
            # 优先尝试 API 解析
            info = get_bilibili_video_info_fallback(url)
            if info:
                return info
    except Exception as e:
        print(f"Custom parser failed: {e}")
        # 失败了继续走 yt-dlp
            
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'cache_dir': '/tmp/yt-dlp-cache', 
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7',
            'Sec-Fetch-Mode': 'navigate',
        }
    }
    
    # 针对 Bilibili 添加 Referer
    if "bilibili.com" in url or "b23.tv" in url:
        ydl_opts['http_headers']['Referer'] = 'https://www.bilibili.com/'
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            formats = []
            # 筛选可用的格式
            if 'formats' in info:
                for f in info['formats']:
                    # 优先寻找包含视频和音频的格式
                    has_video = f.get('vcodec') != 'none'
                    has_audio = f.get('acodec') != 'none'
                    
                    if has_video:
                        note = f.get('format_note') or f.get('resolution') or "unknown"
                        if not has_audio:
                            note += " (无音频)"
                        
                        formats.append({
                            "url": f.get('url'),
                            "ext": f.get('ext'),
                            "format_note": note,
                            "filesize": f.get('filesize'),
                            "format_id": f.get('format_id'),
                            "has_audio": has_audio,
                            "type": "video"
                        })
                    elif has_audio:
                        formats.append({
                            "url": f.get('url'),
                            "ext": f.get('ext'),
                            "format_note": "仅音频",
                            "filesize": f.get('filesize'),
                            "format_id": f.get('format_id'),
                            "has_audio": True,
                            "type": "audio"
                        })

            # 如果没有找到任何视频格式，尝试直接使用 info
            if not formats and info.get('url'):
                 formats.append({
                    "url": info.get('url'),
                    "ext": info.get('ext', 'mp4'),
                    "format_note": "Default",
                    "filesize": info.get('filesize'),
                    "has_audio": True # 假设默认的是完整的
                })

            # 排序：优先有音频的，然后按分辨率/质量排序
            # 这里简单处理：有音频的排前面，然后倒序（通常 yt-dlp 也是质量好的在后）
            formats.sort(key=lambda x: (x.get('has_audio', False), x.get('filesize') or 0), reverse=True)

            return {
                "title": info.get('title'),
                "thumbnail": info.get('thumbnail'),
                "duration": info.get('duration'),
                "webpage_url": info.get('webpage_url'),
                "formats": formats
            }
            
    except Exception as e:
        print(f"Error: {e}")
        # 如果自定义解析和 yt-dlp 都失败了，直接抛出异常
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/download_merged")
async def download_merged(request: DownloadRequest, background_tasks: BackgroundTasks):
    """
    下载并合并视频（如果需要）
    注意：在 Vercel 等 Serverless 环境可能会因为超时或缺少 ffmpeg 而失败
    """
    url = request.url
    
    # 清理文件名
    # filename = f"{request.title}.mp4" 
    # safe_title = re.sub(r'[\\/*?:"<>|]', "", request.title).strip()
    # if not safe_title:
    #     safe_title = "video"
    
    # 之前已经有 safe_title 定义，这里整理一下
    safe_title = "".join([c for c in request.title if c.isalpha() or c.isdigit() or c in (' ', '-', '_', '.')]).rstrip()
    if not safe_title:
        safe_title = "video"
    filename = f"{safe_title}.mp4"
    file_path = os.path.join(DOWNLOAD_DIR, filename)
    
    # 如果文件已存在，先删除（避免冲突）
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except:
            pass
    
    # 针对全民K歌等直链，如果 yt-dlp 不支持，尝试手动解析获取直链进行下载
    # 简单的判断逻辑：如果是全民K歌链接
    if "kg.qq.com" in url:
         info = get_kg_video_info(url)
         if info and info.get('formats'):
             # 获取直链
             direct_url = info['formats'][0]['url']
             print(f"Detected KG URL, resolved to direct URL: {direct_url}")
             
             # 如果是直链，直接流式传输，不使用 yt-dlp 下载到本地（节省 Vercel 资源）
             # 复用 proxy_download 的逻辑
             headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
             }
             try:
                r = requests.get(direct_url, headers=headers, stream=True)
                return StreamingResponse(
                    r.iter_content(chunk_size=8192),
                    media_type=r.headers.get("Content-Type", "video/mp4"),
                    headers={"Content-Disposition": f'attachment; filename="{urllib.parse.quote(filename)}"'}
                )
             except Exception as e:
                 print(f"Stream Error: {e}")
                 raise HTTPException(status_code=400, detail=f"Stream failed: {e}")

    # 针对 Bilibili 备用逻辑
     if "bilibili.com" in url or "b23.tv" in url:
          # 只有当非直链下载时才尝试 fallback
          fallback_info = get_bilibili_video_info_fallback(url)
          if fallback_info and fallback_info.get('formats'):
              direct_url = fallback_info['formats'][0]['url']
              print(f"Detected Bili URL, resolved to fallback direct URL: {direct_url}")
              headers = {
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                 "Referer": "https://www.bilibili.com/"
              }
              try:
                 r = requests.get(direct_url, headers=headers, stream=True)
                 return StreamingResponse(
                     r.iter_content(chunk_size=8192),
                     media_type=r.headers.get("Content-Type", "video/mp4"),
                     headers={"Content-Disposition": f'attachment; filename="{urllib.parse.quote(filename)}"'}
                 )
              except Exception as e:
                  print(f"Bili Stream Error: {e}")
 
     # 检查 ffmpeg 是否可用
     ffmpeg_available = False
    try:
        # 简单检查 ffmpeg 命令
        import subprocess
        subprocess.run(['ffmpeg', '-version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ffmpeg_available = True
    except:
        # 尝试检查 bin 目录
        if os.path.exists("bin/ffmpeg"):
             ffmpeg_available = True
    
    ydl_opts = {
        'outtmpl': os.path.join(DOWNLOAD_DIR, f'{safe_title}.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'cache_dir': '/tmp/yt-dlp-cache', # Vercel 必须指定可写缓存目录
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        }
    }

    if ffmpeg_available:
        ydl_opts['format'] = 'bestvideo+bestaudio/best'
        ydl_opts['merge_output_format'] = 'mp4'
    else:
        # 如果没有 ffmpeg，回退到 best，不尝试合并
        print("FFmpeg not found, falling back to 'best' format without merge.")
        ydl_opts['format'] = 'best'
    
    try:
        # 使用 yt-dlp 下载并合并
        print(f"Starting download for {url}...")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        
        # 查找实际生成的文件（因为 outtmpl 可能会有不同的后缀，如果 merge 成功应该是 mp4）
        # 如果 merge 失败或不需要 merge，可能是 mkv 或其他
        # 这里简单遍历查找匹配文件名的
        
        target_file = None
        for f in os.listdir(DOWNLOAD_DIR):
            if f.startswith(safe_title):
                target_file = os.path.join(DOWNLOAD_DIR, f)
                break
        
        if not target_file or not os.path.exists(target_file):
             raise HTTPException(status_code=500, detail="Download failed: file not found")
             
        print(f"Download finished: {target_file}")
        
        # 设置后台任务：发送完文件后清理
        # 注意：对于大文件，浏览器下载可能需要时间，立即清理会导致下载中断
        # 这里我们暂时不自动清理，或者设置一个较长的延迟（复杂），或者让用户手动管理
        # 为简单起见，这里不立即清理，而是让它保留在服务器 downloads 文件夹中
        # 实际生产环境应该有定期清理任务
        
        # background_tasks.add_task(cleanup_file, target_file) 
        
        return FileResponse(
            path=target_file, 
            filename=os.path.basename(target_file),
            media_type='application/octet-stream'
        )

    except Exception as e:
        print(f"Merge Download Error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/proxy_download")
async def proxy_download(url: str, name: str = "video.mp4"):
    """
    代理下载接口，用于绕过 Referer 限制
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    
    # 针对不同平台设置 Referer
    if "bilibili" in url or "bilivideo" in url:
        headers["Referer"] = "https://www.bilibili.com/"
    elif "youtube" in url or "googlevideo" in url:
         headers["Referer"] = "https://www.youtube.com/"
    
    try:
        # 使用 stream=True 进行流式传输
        r = requests.get(url, headers=headers, stream=True)
        
        return StreamingResponse(
            r.iter_content(chunk_size=8192),
            media_type=r.headers.get("Content-Type", "video/mp4"),
            headers={"Content-Disposition": f'attachment; filename="{urllib.parse.quote(name)}"'}
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Download failed: {str(e)}")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
