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
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# 临时下载目录
DOWNLOAD_DIR = "downloads"
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

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
        
        # 尝试提取播放地址
        # 全民K歌页面通常包含 playurl 变量
        play_url_match = re.search(r'playurl\s*[:=]\s*["\'](.*?)["\']', content)
        if not play_url_match:
            play_url_match = re.search(r'src\s*=\s*["\'](http.*?mp4.*?)["\']', content)
            
        # 提取标题
        title_match = re.search(r'<title>(.*?)</title>', content)
        title = title_match.group(1) if title_match else "全民K歌视频"
        
        if play_url_match:
            video_url = play_url_match.group(1)
            return {
                "title": title,
                "thumbnail": "", # 可以尝试提取封面，这里先留空
                "formats": [{
                    "url": video_url,
                    "ext": "mp4",
                    "format_note": "Default",
                    "filesize": 0 # 未知大小
                }]
            }
        return None
    except Exception as e:
        print(f"KG Parse Error: {e}")
        return None

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/api/resolve")
async def resolve_video(request: VideoRequest):
    url = request.url
    
    # 全民K歌特殊处理 (如果 yt-dlp 失败或作为优先尝试)
    if "kg.qq.com" in url:
        info = get_kg_video_info(url)
        if info:
            return info
            
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        # 'format': 'best', # 移除强制 format，避免报错
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        }
    }
    
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
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/download_merged")
async def download_merged(request: DownloadRequest, background_tasks: BackgroundTasks):
    """
    下载并合并视频（如果需要）
    """
    url = request.url
    # 清理文件名
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

    ydl_opts = {
        'format': 'bestvideo+bestaudio/best', # 尝试合并最佳视频+最佳音频
        'outtmpl': os.path.join(DOWNLOAD_DIR, f'{safe_title}.%(ext)s'),
        'merge_output_format': 'mp4',
        'quiet': True,
        'no_warnings': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        }
    }
    
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
