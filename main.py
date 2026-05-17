from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import yt_dlp
import httpx
import os
import re

app = FastAPI(title="SaveIt Pro API", version="3.0.0")

# ✅ Fixed CORS — removed allow_credentials (was blocking file:// requests)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")
COOKIES_FILE = "cookies.txt"


class InfoRequest(BaseModel):
    url: str


def detect_platform(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:    return "youtube"
    if "instagram.com" in u:                      return "instagram"
    if "pinterest.com" in u or "pin.it" in u:     return "pinterest"
    if "facebook.com" in u or "fb.watch" in u:    return "facebook"
    if "twitter.com" in u or "x.com" in u:        return "twitter"
    if "tiktok.com" in u:                         return "tiktok"
    return "other"


def format_size(b) -> str:
    if not b: return "~"
    mb = int(b) / (1024 * 1024)
    return f"{mb/1024:.1f} GB" if mb >= 1000 else f"{mb:.0f} MB"


def format_duration(s) -> str:
    if not s: return "0:00"
    m, sec = divmod(int(s), 60)
    h, m   = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def format_views(v) -> str:
    if not v: return "—"
    if v >= 1_000_000: return f"{v/1_000_000:.1f}M"
    if v >= 1_000:     return f"{v/1_000:.1f}K"
    return str(v)


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


@app.get("/")
def root():
    return {
        "status":   "SaveIt Pro API v3 ✅",
        "rapidapi": "✅ Key set" if RAPIDAPI_KEY else "❌ Missing",
        "cookies":  "✅ Loaded"  if os.path.exists(COOKIES_FILE) else "❌ Not found",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


# ✅ Proxy Download — forces browser to download instead of opening in new tab
@app.get("/api/proxy-download")
async def proxy_download(
    url:      str = Query(..., description="Direct video/audio URL to proxy"),
    filename: str = Query("video.mp4", description="Download filename"),
):
    """
    Proxies any direct URL through the server so browser downloads it
    instead of opening in a new tab (fixes YouTube CDN CORS issue).
    """
    try:
        async def stream_from_url():
            async with httpx.AsyncClient(
                timeout=300,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"}
            ) as client:
                async with client.stream("GET", url) as resp:
                    async for chunk in resp.aiter_bytes(256 * 1024):
                        yield chunk

        safe_name = re.sub(r'[^\w.\-]', '_', filename)[:100]
        return StreamingResponse(
            stream_from_url(),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_name}"',
                "Cache-Control": "no-cache",
            }
        )
    except Exception as e:
        raise HTTPException(500, f"Proxy download failed: {str(e)[:200]}")


@app.post("/api/info")
async def get_video_info(req: InfoRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "URL is required")

    platform = detect_platform(url)

    # Instagram, Facebook, Pinterest, Twitter, TikTok — yt-dlp
    opts = {**get_ytdlp_opts(), "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(422, f"Could not fetch video: {str(e)[:200]}")
    except Exception as e:
        raise HTTPException(500, f"Server error: {str(e)[:200]}")

    formats   = info.get("formats", [])
    qualities = []
    seen_res  = set()

    for f in reversed(formats):
        h      = f.get("height")
        ext    = f.get("ext", "mp4")
        size   = f.get("filesize") or f.get("filesize_approx")
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")

        if h and vcodec != "none" and h not in seen_res:
            seen_res.add(h)
            qualities.append({
                "format_id":    f.get("format_id"),
                "resolution":   f"{h}p HD" if h >= 1080 else f"{h}p",
                "height":       h,
                "ext":          ext if ext != "none" else "mp4",
                "size":         format_size(size),
                "type":         "video",
                "has_audio":    acodec != "none",
                "kind":         "combined",
                "download_url": "",
            })

    qualities.sort(key=lambda x: x["height"], reverse=True)
    qualities.append({
        "format_id": "bestaudio", "resolution": "MP3", "height": 0,
        "ext": "mp3", "size": "~5-10 MB", "type": "audio",
        "has_audio": True, "kind": "audio", "download_url": "",
    })

    return {
        "success":          True,
        "platform":         platform,
        "title":            info.get("title", "Untitled"),
        "thumbnail":        info.get("thumbnail", ""),
        "duration":         format_duration(info.get("duration")),
        "duration_seconds": info.get("duration", 0),
        "uploader":         info.get("uploader") or info.get("channel", "Unknown"),
        "views":            format_views(info.get("view_count")),
        "description":      (info.get("description", "") or "")[:300],
        "upload_date":      info.get("upload_date", ""),
        "qualities":        qualities[:10],
        "original_url":     url,
    }


@app.get("/api/download")
async def download_video(
    url:       str = Query(...),
    format_id: str = Query("bestaudio"),
    quality:   str = Query("MP3"),
):
    """Download via yt-dlp — for Instagram, Facebook, Pinterest"""
    is_audio = format_id == "bestaudio" or quality == "MP3"

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
        h = quality.replace("p", "").replace(" HD", "").strip()
        opts = {
            **get_ytdlp_opts(),
            "format": f"{format_id}+bestaudio/best[height<={h}]/best",
            "outtmpl": "/tmp/%(title)s.%(ext)s",
            "merge_output_format": "mp4",
        }
        content_type = "video/mp4"
        ext = "mp4"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info     = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if is_audio:
                filename = filename.rsplit(".", 1)[0] + ".mp3"
    except Exception as e:
        raise HTTPException(500, f"Download failed: {str(e)[:200]}")

    if not os.path.exists(filename):
        base = filename.rsplit(".", 1)[0]
        for ext2 in ["mp4", "mp3", "webm", "mkv", "m4a"]:
            if os.path.exists(f"{base}.{ext2}"):
                filename = f"{base}.{ext2}"
                break
        else:
            raise HTTPException(500, "Downloaded file not found")

    file_size = os.path.getsize(filename)
    safe      = re.sub(r'[^\w\s-]', '', info.get("title", "video"))[:60].strip()

    def stream():
        with open(filename, "rb") as f:
            while chunk := f.read(256 * 1024):
                yield chunk
        try: os.remove(filename)
        except: pass

    return StreamingResponse(stream(), media_type=content_type, headers={
        "Content-Disposition": f'attachment; filename="{safe}.{ext}"',
        "Content-Length":      str(file_size),
    })


@app.get("/api/thumbnail")
async def proxy_thumbnail(url: str = Query(...)):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url)
            return StreamingResponse(
                iter([r.content]),
                media_type=r.headers.get("content-type", "image/jpeg")
            )
    except:
        raise HTTPException(404, "Thumbnail not found")
