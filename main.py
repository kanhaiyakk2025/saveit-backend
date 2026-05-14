from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import yt_dlp
import httpx
import os
import re

app = FastAPI(title="SaveIt Pro API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")
COOKIES_FILE = "cookies.txt"


class InfoRequest(BaseModel):
    url: str


def is_youtube(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


def detect_platform(url: str) -> str:
    url = url.lower()
    if "youtube.com" in url or "youtu.be" in url:   return "youtube"
    elif "instagram.com" in url:                     return "instagram"
    elif "pinterest.com" in url or "pin.it" in url:  return "pinterest"
    elif "facebook.com" in url or "fb.watch" in url: return "facebook"
    elif "twitter.com" in url or "x.com" in url:    return "twitter"
    elif "tiktok.com" in url:                        return "tiktok"
    return "other"


def extract_youtube_id(url: str) -> str:
    patterns = [
        r"youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})",
        r"youtu\.be/([a-zA-Z0-9_-]{11})",
        r"youtube\.com/shorts/([a-zA-Z0-9_-]{11})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return ""


def format_size(bytes_val) -> str:
    if not bytes_val:
        return "Unknown"
    mb = bytes_val / (1024 * 1024)
    return f"{mb/1024:.1f} GB" if mb >= 1000 else f"{mb:.1f} MB"


def format_duration(seconds) -> str:
    if not seconds:
        return "0:00"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def format_views(views) -> str:
    if not views: return "—"
    if views >= 1_000_000: return f"{views/1_000_000:.1f}M"
    if views >= 1_000:     return f"{views/1_000:.1f}K"
    return str(views)


def get_ytdlp_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 5,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        },
    }
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


async def youtube_info_rapidapi(video_id: str) -> dict:
    url = f"https://youtube-mp36.p.rapidapi.com/dl?id={video_id}"
    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)
        return resp.json()


@app.get("/")
def root():
    return {
        "status": "SaveIt Pro API chal raha hai ✅",
        "version": "1.0.0",
        "rapidapi": "✅ Key set" if RAPIDAPI_KEY else "❌ Key missing",
        "cookies": "✅ Loaded" if os.path.exists(COOKIES_FILE) else "❌ Not found",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/info")
async def get_video_info(req: InfoRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL nahi diya")

    platform = detect_platform(url)

    # ── YOUTUBE — RapidAPI ──
    if is_youtube(url) and RAPIDAPI_KEY:
        video_id = extract_youtube_id(url)
        if not video_id:
            raise HTTPException(status_code=400, detail="YouTube video ID nahi mila")

        try:
            data = await youtube_info_rapidapi(video_id)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"RapidAPI error: {str(e)[:150]}")

        if data.get("status") != "ok":
            raise HTTPException(status_code=422, detail=f"YouTube fetch fail: {data.get('msg', 'Unknown error')}")

        filesize = data.get("filesize", 0)
        size_str = f"{filesize // (1024*1024)} MB" if filesize else "~5-10 MB"

        qualities = [
            {
                "format_id": "rapidapi_mp3",
                "resolution": "MP3",
                "height": 0,
                "ext": "mp3",
                "size": size_str,
                "type": "audio",
                "has_audio": True,
                "download_url": data.get("link", ""),
            },
            {
                "format_id": "rapidapi_360p",
                "resolution": "360p",
                "height": 360,
                "ext": "mp4",
                "size": "~50-100 MB",
                "type": "video",
                "has_audio": True,
                "download_url": "",
            },
        ]

        return {
            "success": True,
            "platform": "youtube",
            "title": data.get("title", "YouTube Video"),
            "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "duration": data.get("duration", "—"),
            "duration_seconds": 0,
            "uploader": "YouTube",
            "views": "—",
            "description": "",
            "upload_date": "",
            "qualities": qualities,
            "original_url": url,
            "video_id": video_id,
        }

    # ── OTHER PLATFORMS — yt-dlp ──
    opts = {**get_ytdlp_opts(), "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail=f"Video fetch nahi hua: {str(e)[:200]}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)[:200]}")

    formats = info.get("formats", [])
    qualities = []
    seen_res = set()

    for f in reversed(formats):
        height = f.get("height")
        ext = f.get("ext", "mp4")
        filesize = f.get("filesize") or f.get("filesize_approx")
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")

        if height and vcodec != "none" and height not in seen_res:
            label = f"{height}p HD" if height >= 1080 else f"{height}p"
            seen_res.add(height)
            qualities.append({
                "format_id": f.get("format_id"),
                "resolution": label,
                "height": height,
                "ext": ext if ext != "none" else "mp4",
                "size": format_size(filesize),
                "type": "video",
                "has_audio": acodec != "none",
                "download_url": "",
            })

    qualities.sort(key=lambda x: x["height"], reverse=True)
    qualities.append({
        "format_id": "bestaudio",
        "resolution": "MP3",
        "height": 0,
        "ext": "mp3",
        "size": "~5-10 MB",
        "type": "audio",
        "has_audio": True,
        "download_url": "",
    })

    return {
        "success": True,
        "platform": platform,
        "title": info.get("title", "Untitled"),
        "thumbnail": info.get("thumbnail", ""),
        "duration": format_duration(info.get("duration")),
        "duration_seconds": info.get("duration", 0),
        "uploader": info.get("uploader") or info.get("channel", "Unknown"),
        "views": format_views(info.get("view_count")),
        "description": (info.get("description", "") or "")[:300],
        "upload_date": info.get("upload_date", ""),
        "qualities": qualities[:8],
        "original_url": url,
    }


@app.get("/api/download")
async def download_video(
    url: str = Query(...),
    format_id: str = Query("bestvideo+bestaudio"),
    quality: str = Query("720p"),
):
    if not url:
        raise HTTPException(status_code=400, detail="URL required hai")

    is_audio = format_id == "bestaudio" or quality == "MP3"

    # ── YOUTUBE MP3 via RapidAPI ──
    if is_youtube(url) and RAPIDAPI_KEY and format_id == "rapidapi_mp3":
        video_id = extract_youtube_id(url)
        if not video_id:
            raise HTTPException(status_code=400, detail="Video ID nahi mila")

        try:
            data = await youtube_info_rapidapi(video_id)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"RapidAPI error: {str(e)}")

        dl_link = data.get("link", "")
        if not dl_link:
            raise HTTPException(status_code=500, detail="Download link nahi mila")

        async def rapidapi_stream():
            async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                async with client.stream("GET", dl_link) as resp:
                    async for chunk in resp.aiter_bytes(1024 * 256):
                        yield chunk

        title = data.get("title", "audio")
        safe_title = re.sub(r'[^\w\s-]', '', title)[:60].strip()

        return StreamingResponse(
            rapidapi_stream(),
            media_type="audio/mpeg",
            headers={"Content-Disposition": f'attachment; filename="{safe_title}.mp3"'}
        )

    # ── YOUTUBE 360p VIDEO ──
    if is_youtube(url) and format_id == "rapidapi_360p":
        opts = {
            **get_ytdlp_opts(),
            "format": "best[height<=360]/worst",
            "outtmpl": "/tmp/%(title)s.%(ext)s",
            "extractor_args": {"youtube": {"player_client": ["android_embedded"]}},
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Video download fail: {str(e)[:200]}")

        if not os.path.exists(filename):
            raise HTTPException(status_code=500, detail="File nahi mila")

        safe_title = re.sub(r'[^\w\s-]', '', info.get("title", "video"))[:60].strip()
        file_size = os.path.getsize(filename)

        def stream():
            with open(filename, "rb") as f:
                while chunk := f.read(1024 * 256):
                    yield chunk
            try: os.remove(filename)
            except: pass

        return StreamingResponse(
            stream(),
            media_type="video/mp4",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_title}.mp4"',
                "Content-Length": str(file_size),
            }
        )

    # ── OTHER PLATFORMS — yt-dlp ──
    if is_audio:
        opts = {
            **get_ytdlp_opts(),
            "format": "bestaudio/best",
            "outtmpl": "/tmp/%(title)s.%(ext)s",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        }
        content_type = "audio/mpeg"
        ext = "mp3"
    else:
        height = quality.replace("p", "").replace(" HD", "")
        opts = {
            **get_ytdlp_opts(),
            "format": f"{format_id}+bestaudio/best[height<={height}]/best",
            "outtmpl": "/tmp/%(title)s.%(ext)s",
            "merge_output_format": "mp4",
        }
        content_type = "video/mp4"
        ext = "mp4"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if is_audio:
                filename = filename.rsplit(".", 1)[0] + ".mp3"
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download fail: {str(e)[:200]}")

    if not os.path.exists(filename):
        base = filename.rsplit(".", 1)[0]
        for possible_ext in ["mp4", "mp3", "webm", "mkv", "m4a"]:
            candidate = f"{base}.{possible_ext}"
            if os.path.exists(candidate):
                filename = candidate
                break
        else:
            raise HTTPException(status_code=500, detail="File nahi mila")

    file_size = os.path.getsize(filename)
    safe_title = re.sub(r'[^\w\s-]', '', info.get("title", "video"))[:60].strip()

    def file_stream():
        with open(filename, "rb") as f:
            while chunk := f.read(1024 * 256):
                yield chunk
        try: os.remove(filename)
        except: pass

    return StreamingResponse(
        file_stream(),
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{safe_title}.{ext}"',
            "Content-Length": str(file_size),
        }
    )


@app.get("/api/thumbnail")
async def proxy_thumbnail(url: str = Query(...)):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            return StreamingResponse(
                iter([resp.content]),
                media_type=resp.headers.get("content-type", "image/jpeg")
            )
    except Exception:
        raise HTTPException(status_code=404, detail="Thumbnail nahi mila")
