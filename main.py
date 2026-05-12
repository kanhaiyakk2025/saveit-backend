from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
import yt_dlp
import httpx
import asyncio
import os
import re

app = FastAPI(title="SaveIt Pro API", version="1.0.0")

# ✅ CORS — frontend se connect hone ke liye
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Production mein apna domain dalo
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ──────────────────────────────────────────────
# MODELS
# ──────────────────────────────────────────────

class InfoRequest(BaseModel):
    url: str

# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────

def detect_platform(url: str) -> str:
    url = url.lower()
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    elif "instagram.com" in url:
        return "instagram"
    elif "pinterest.com" in url or "pin.it" in url:
        return "pinterest"
    elif "facebook.com" in url or "fb.watch" in url:
        return "facebook"
    elif "twitter.com" in url or "x.com" in url:
        return "twitter"
    elif "tiktok.com" in url:
        return "tiktok"
    return "other"

def format_size(bytes_val) -> str:
    if not bytes_val:
        return "Unknown"
    mb = bytes_val / (1024 * 1024)
    if mb >= 1000:
        return f"{mb/1024:.1f} GB"
    return f"{mb:.1f} MB"

def format_duration(seconds) -> str:
    if not seconds:
        return "0:00"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"

def format_views(views) -> str:
    if not views:
        return "—"
    if views >= 1_000_000:
        return f"{views/1_000_000:.1f}M"
    if views >= 1_000:
        return f"{views/1_000:.1f}K"
    return str(views)

# ──────────────────────────────────────────────
# ROUTES
# ──────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "SaveIt Pro API chal raha hai ✅", "version": "1.0.0"}

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/info")
async def get_video_info(req: InfoRequest):
    """
    URL se video info fetch karo:
    title, thumbnail, duration, available qualities
    """
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL nahi diya")

    platform = detect_platform(url)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail=f"Video fetch nahi hua: {str(e)[:200]}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)[:200]}")

    # Available video qualities
    formats = info.get("formats", [])
    qualities = []
    seen_res = set()

    # Video formats
    for f in reversed(formats):
        height = f.get("height")
        ext = f.get("ext", "mp4")
        filesize = f.get("filesize") or f.get("filesize_approx")
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")

        if height and vcodec != "none" and height not in seen_res:
            label = f"{height}p"
            if height >= 1080:
                label = f"{height}p HD"
            seen_res.add(height)
            qualities.append({
                "format_id": f.get("format_id"),
                "resolution": label,
                "height": height,
                "ext": ext if ext != "none" else "mp4",
                "size": format_size(filesize),
                "type": "video",
                "has_audio": acodec != "none",
            })

    # Sort by quality (high to low)
    qualities.sort(key=lambda x: x["height"], reverse=True)

    # MP3 / Audio option hamesha add karo
    qualities.append({
        "format_id": "bestaudio",
        "resolution": "MP3",
        "height": 0,
        "ext": "mp3",
        "size": "~5-10 MB",
        "type": "audio",
        "has_audio": True,
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
        "qualities": qualities[:8],  # Max 8 options
        "original_url": url,
    }


@app.get("/api/download")
async def download_video(
    url: str = Query(..., description="Video URL"),
    format_id: str = Query("bestvideo+bestaudio", description="Format ID from /api/info"),
    quality: str = Query("720p", description="Quality label for filename"),
):
    """
    Video stream karke download karo
    """
    if not url:
        raise HTTPException(status_code=400, detail="URL required hai")

    is_audio = format_id == "bestaudio" or quality == "MP3"

    if is_audio:
        ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
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
        ydl_opts = {
            "format": f"{format_id}+bestaudio/best[height<={quality.replace('p','').replace(' HD','')}]/best",
            "quiet": True,
            "no_warnings": True,
            "outtmpl": "/tmp/%(title)s.%(ext)s",
            "merge_output_format": "mp4",
        }
        content_type = "video/mp4"
        ext = "mp4"

    # Download to temp
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            # Audio ka extension change hota hai
            if is_audio:
                filename = filename.rsplit(".", 1)[0] + ".mp3"
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download fail: {str(e)[:200]}")

    if not os.path.exists(filename):
        # Possible filename mismatch — search karo
        base = filename.rsplit(".", 1)[0]
        for possible_ext in ["mp4", "mp3", "webm", "mkv", "m4a"]:
            candidate = f"{base}.{possible_ext}"
            if os.path.exists(candidate):
                filename = candidate
                break
        else:
            raise HTTPException(status_code=500, detail="Downloaded file nahi mila")

    file_size = os.path.getsize(filename)
    safe_title = re.sub(r'[^\w\s-]', '', info.get("title", "video"))[:60].strip()
    download_name = f"{safe_title}.{ext}"

    def file_stream():
        with open(filename, "rb") as f:
            while chunk := f.read(1024 * 256):  # 256KB chunks
                yield chunk
        # Cleanup
        try:
            os.remove(filename)
        except Exception:
            pass

    return StreamingResponse(
        file_stream(),
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{download_name}"',
            "Content-Length": str(file_size),
            "X-Platform": detect_platform(url),
        }
    )


@app.get("/api/thumbnail")
async def proxy_thumbnail(url: str = Query(...)):
    """
    Thumbnail image proxy — CORS issue fix karta hai
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            return StreamingResponse(
                iter([resp.content]),
                media_type=resp.headers.get("content-type", "image/jpeg")
            )
    except Exception:
        raise HTTPException(status_code=404, detail="Thumbnail nahi mila")
