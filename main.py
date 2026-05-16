from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import yt_dlp
import httpx
import os
import re

app = FastAPI(title="SaveIt Pro API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

RAPIDAPI_KEY  = os.environ.get("RAPIDAPI_KEY", "")
COOKIES_FILE  = "cookies.txt"

class InfoRequest(BaseModel):
    url: str

# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────

def is_youtube(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url

def detect_platform(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:    return "youtube"
    if "instagram.com" in u:                      return "instagram"
    if "pinterest.com" in u or "pin.it" in u:     return "pinterest"
    if "facebook.com" in u or "fb.watch" in u:    return "facebook"
    if "twitter.com" in u or "x.com" in u:        return "twitter"
    if "tiktok.com" in u:                         return "tiktok"
    return "other"

def extract_youtube_id(url: str) -> str:
    for p in [
        r"youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})",
        r"youtu\.be/([a-zA-Z0-9_-]{11})",
        r"youtube\.com/shorts/([a-zA-Z0-9_-]{11})",
        r"youtube\.com/embed/([a-zA-Z0-9_-]{11})",
    ]:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return ""

def format_size(b) -> str:
    if not b: return "~"
    mb = b / (1024 * 1024)
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
        "quiet": True, "no_warnings": True, "noplaylist": True, "retries": 5,
        "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
    }
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts

# ──────────────────────────────────────────────
# RAPIDAPI CALLS
# ──────────────────────────────────────────────

async def ytstream_info(video_id: str) -> dict:
    """YTStream API — video formats with direct URLs"""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"https://ytstream-download-youtube-videos.p.rapidapi.com/dl?id={video_id}",
            headers={
                "X-RapidAPI-Key":  RAPIDAPI_KEY,
                "X-RapidAPI-Host": "ytstream-download-youtube-videos.p.rapidapi.com",
            }
        )
        return r.json()

async def mp36_info(video_id: str) -> dict:
    """MP36 API — MP3 download link"""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"https://youtube-mp36.p.rapidapi.com/dl?id={video_id}",
            headers={
                "X-RapidAPI-Key":  RAPIDAPI_KEY,
                "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com",
            }
        )
        return r.json()

# ──────────────────────────────────────────────
# ROUTES
# ──────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "status":  "SaveIt Pro API v2 ✅",
        "rapidapi": "✅ Key set" if RAPIDAPI_KEY else "❌ Missing",
        "cookies":  "✅ Loaded"  if os.path.exists(COOKIES_FILE) else "❌ Not found",
    }

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/info")
async def get_video_info(req: InfoRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "URL nahi diya")

    # ── YOUTUBE ──
    if is_youtube(url) and RAPIDAPI_KEY:
        vid = extract_youtube_id(url)
        if not vid:
            raise HTTPException(400, "YouTube video ID nahi mila")

        try:
            data = await ytstream_info(vid)
        except Exception as e:
            raise HTTPException(500, f"YTStream error: {e}")

        if not data.get("title"):
            raise HTTPException(422, f"YouTube fetch fail: {data}")

        # Parse formats — direct download URLs
        formats = data.get("formats") or {}
        qualities = []

        # Resolution map
        res_map = {
            "2160": "4K", "1440": "1440p", "1080": "1080p HD",
            "720": "720p", "480": "480p", "360": "360p", "240": "240p", "144": "144p"
        }

        for key, fmts in formats.items():
            if not isinstance(fmts, list):
                fmts = [fmts]
            for f in fmts:
                height = str(f.get("height") or f.get("qualityLabel","").replace("p","").split()[0] or "")
                dl_url  = f.get("url","")
                if not dl_url or not height:
                    continue
                label = res_map.get(height, f"{height}p")
                size  = format_size(f.get("contentLength") or f.get("filesize"))
                qualities.append({
                    "format_id":    f"ytstream_{height}",
                    "resolution":   label,
                    "height":       int(height) if height.isdigit() else 0,
                    "ext":          "mp4",
                    "size":         size,
                    "type":         "video",
                    "has_audio":    True,
                    "download_url": dl_url,
                })

        # Deduplicate by height
        seen = set()
        unique = []
        for q in sorted(qualities, key=lambda x: x["height"], reverse=True):
            if q["height"] not in seen:
                seen.add(q["height"])
                unique.append(q)

        # MP3 — mp36 API se
        try:
            mp3data = await mp36_info(vid)
            if mp3data.get("status") == "ok":
                sz = mp3data.get("filesize", 0)
                unique.append({
                    "format_id":    "rapidapi_mp3",
                    "resolution":   "MP3",
                    "height":       0,
                    "ext":          "mp3",
                    "size":         f"{sz//(1024*1024)} MB" if sz else "~5-10 MB",
                    "type":         "audio",
                    "has_audio":    True,
                    "download_url": mp3data.get("link", ""),
                })
        except:
            unique.append({
                "format_id": "rapidapi_mp3", "resolution": "MP3",
                "height": 0, "ext": "mp3", "size": "~5-10 MB",
                "type": "audio", "has_audio": True, "download_url": "",
            })

        thumb = data.get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        if isinstance(thumb, list):
            thumb = thumb[-1].get("url", "") if thumb else ""

        return {
            "success": True, "platform": "youtube",
            "title":    data.get("title", "YouTube Video"),
            "thumbnail": thumb,
            "duration":  data.get("lengthSeconds","") and format_duration(int(data["lengthSeconds"])),
            "duration_seconds": int(data.get("lengthSeconds", 0)),
            "uploader":  data.get("author","YouTube"),
            "views":     format_views(int(data.get("viewCount","0") or 0)),
            "description": (data.get("description","") or "")[:300],
            "upload_date": "",
            "qualities": unique[:10],
            "original_url": url,
        }

    # ── OTHER PLATFORMS — yt-dlp ──
    opts = {**get_ytdlp_opts(), "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(422, f"Fetch fail: {str(e)[:200]}")
    except Exception as e:
        raise HTTPException(500, f"Server error: {str(e)[:200]}")

    formats   = info.get("formats", [])
    qualities = []
    seen_res  = set()

    for f in reversed(formats):
        h       = f.get("height")
        ext     = f.get("ext", "mp4")
        size    = f.get("filesize") or f.get("filesize_approx")
        vcodec  = f.get("vcodec","none")
        acodec  = f.get("acodec","none")
        if h and vcodec != "none" and h not in seen_res:
            seen_res.add(h)
            qualities.append({
                "format_id": f.get("format_id"), "resolution": f"{h}p HD" if h>=1080 else f"{h}p",
                "height": h, "ext": ext if ext!="none" else "mp4",
                "size": format_size(size), "type": "video", "has_audio": acodec!="none", "download_url": "",
            })

    qualities.sort(key=lambda x: x["height"], reverse=True)
    qualities.append({
        "format_id":"bestaudio","resolution":"MP3","height":0,
        "ext":"mp3","size":"~5-10 MB","type":"audio","has_audio":True,"download_url":"",
    })

    return {
        "success": True, "platform": detect_platform(url),
        "title":    info.get("title","Untitled"),
        "thumbnail": info.get("thumbnail",""),
        "duration":  format_duration(info.get("duration")),
        "duration_seconds": info.get("duration",0),
        "uploader":  info.get("uploader") or info.get("channel","Unknown"),
        "views":     format_views(info.get("view_count")),
        "description": (info.get("description","") or "")[:300],
        "upload_date": info.get("upload_date",""),
        "qualities": qualities[:10],
        "original_url": url,
    }


@app.get("/api/download")
async def download_video(
    url:       str = Query(...),
    format_id: str = Query("bestaudio"),
    quality:   str = Query("MP3"),
):
    """Only non-YouTube / fallback downloads — YouTube uses direct URLs"""
    if not url:
        raise HTTPException(400, "URL required")

    is_audio = format_id == "bestaudio" or quality == "MP3"

    if is_audio:
        opts = {
            **get_ytdlp_opts(),
            "format": "bestaudio/best",
            "outtmpl": "/tmp/%(title)s.%(ext)s",
            "postprocessors": [{"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"192"}],
        }
        content_type = "audio/mpeg"
        ext = "mp3"
    else:
        h = quality.replace("p","").replace(" HD","").replace("K","").strip()
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
                filename = filename.rsplit(".",1)[0] + ".mp3"
    except Exception as e:
        raise HTTPException(500, f"Download fail: {str(e)[:200]}")

    if not os.path.exists(filename):
        base = filename.rsplit(".",1)[0]
        for ext2 in ["mp4","mp3","webm","mkv","m4a"]:
            if os.path.exists(f"{base}.{ext2}"):
                filename = f"{base}.{ext2}"
                break
        else:
            raise HTTPException(500, "File nahi mila")

    safe = re.sub(r'[^\w\s-]','', info.get("title","video"))[:60].strip()

    def stream():
        with open(filename,"rb") as f:
            while chunk := f.read(256*1024):
                yield chunk
        try: os.remove(filename)
        except: pass

    return StreamingResponse(stream(), media_type=content_type, headers={
        "Content-Disposition": f'attachment; filename="{safe}.{ext}"',
        "Content-Length": str(os.path.getsize(filename)),
    })


@app.get("/api/thumbnail")
async def proxy_thumbnail(url: str = Query(...)):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url)
            return StreamingResponse(iter([r.content]), media_type=r.headers.get("content-type","image/jpeg"))
    except:
        raise HTTPException(404, "Thumbnail nahi mila")
