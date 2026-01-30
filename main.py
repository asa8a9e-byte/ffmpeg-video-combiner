import os
import uuid
import subprocess
import tempfile
from typing import Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import httpx

app = FastAPI(title="FFmpeg Video Combiner API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEMP_DIR = tempfile.gettempdir()
OUTPUT_DIR = os.path.join(TEMP_DIR, "ffmpeg_output")
FONT_DIR = "/usr/share/fonts/truetype"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# === Models ===

class CombineRequest(BaseModel):
    video_url: str
    audio_url: str
    output_format: str = "mp4"

class ImageToVideoRequest(BaseModel):
    image_url: str
    audio_url: str
    output_format: str = "mp4"

class CaptionStyle(BaseModel):
    font: str = "regular"  # regular, bold, extra-bold
    size: int = 24
    color: str = "white"
    outline_color: str = "black"
    outline_width: int = 2
    shadow: bool = True

class Caption(BaseModel):
    text: str
    start_time: float  # seconds
    end_time: float    # seconds
    style: str = "default"  # style name from caption_styles
    position: str = "bottom"  # top, center, bottom

class CaptionStyles(BaseModel):
    default: CaptionStyle = CaptionStyle()
    emphasis: CaptionStyle = CaptionStyle(font="bold", size=28, color="yellow")
    impact: CaptionStyle = CaptionStyle(font="extra-bold", size=32, color="red", outline_color="white")
    gentle: CaptionStyle = CaptionStyle(font="regular", size=22, color="white")

class CombineWithCaptionsRequest(BaseModel):
    video_url: str
    voice_url: Optional[str] = None  # Voice/narration audio (optional)
    audio_url: Optional[str] = None  # BGM (optional)
    captions: list[Caption]
    caption_styles: CaptionStyles = CaptionStyles()
    output_format: str = "mp4"

class CombineResponse(BaseModel):
    success: bool
    job_id: str
    message: str
    output_url: str | None = None

# === Helper Functions ===

async def download_file(url: str, dest_path: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.get(url, follow_redirects=True)
            response.raise_for_status()
            with open(dest_path, "wb") as f:
                f.write(response.content)
        return True
    except Exception as e:
        print(f"Download error: {e}")
        return False

def get_ffmpeg_version() -> str:
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
        return result.stdout.split('\n')[0]
    except Exception:
        return "FFmpeg not found"

def get_font_file(font_type: str) -> str:
    """フォントタイプに応じたフォントファイルを返す"""
    # Noto Sans CJK JP フォントマッピング
    font_map = {
        "regular": ["NotoSansCJK-Regular.ttc", "NotoSansCJKjp-Regular.otf"],
        "bold": ["NotoSansCJK-Bold.ttc", "NotoSansCJKjp-Bold.otf"],
        "extra-bold": ["NotoSansCJK-Black.ttc", "NotoSansCJKjp-Black.otf"],
    }
    font_files = font_map.get(font_type, font_map["regular"])

    # 複数のパスを試す
    base_paths = [
        "/usr/share/fonts/opentype/noto",
        "/usr/share/fonts/truetype/noto",
        "/usr/share/fonts/noto-cjk",
        "/usr/share/fonts/opentype/noto-cjk",
    ]

    for base_path in base_paths:
        for font_file in font_files:
            full_path = f"{base_path}/{font_file}"
            if os.path.exists(full_path):
                print(f"Found font: {full_path}")
                return full_path

    # フォールバック: fc-matchで検索
    try:
        import subprocess
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", "Noto Sans CJK JP"],
            capture_output=True, text=True
        )
        if result.returncode == 0 and result.stdout.strip():
            font_path = result.stdout.strip()
            print(f"Found font via fc-match: {font_path}")
            return font_path
    except Exception as e:
        print(f"fc-match failed: {e}")

    # 最終フォールバック
    fallback = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if os.path.exists(fallback):
        return fallback

    return "DejaVuSans"  # FFmpeg default

def get_position_y(position: str, video_height: int = 720) -> str:
    """位置に応じたY座標を返す"""
    positions = {
        "top": "50",
        "center": f"(h-text_h)/2",
        "bottom": f"h-text_h-50",
    }
    return positions.get(position, positions["bottom"])

def escape_text_for_ffmpeg(text: str) -> str:
    """FFmpeg drawtext用にテキストをエスケープ"""
    # FFmpegのdrawtextフィルタ用エスケープ
    text = text.replace("\\", "\\\\")
    text = text.replace(":", "\\:")
    text = text.replace("'", "\\'")
    text = text.replace("[", "\\[")
    text = text.replace("]", "\\]")
    return text

def build_drawtext_filter(captions: list[Caption], styles: CaptionStyles) -> str:
    """複数のテロップ用のdrawtextフィルタを構築"""
    filters = []

    style_map = {
        "default": styles.default,
        "emphasis": styles.emphasis,
        "impact": styles.impact,
        "gentle": styles.gentle,
    }

    for caption in captions:
        style = style_map.get(caption.style, styles.default)
        escaped_text = escape_text_for_ffmpeg(caption.text)
        font_file = get_font_file(style.font)
        y_pos = get_position_y(caption.position)

        # 影の設定
        shadow_settings = ""
        if style.shadow:
            shadow_settings = ":shadowcolor=black@0.5:shadowx=2:shadowy=2"

        # フォントファイルが存在する場合のみ指定
        if font_file and os.path.exists(font_file):
            font_setting = f"fontfile='{font_file}'"
        else:
            # フォントがない場合はfontを使用
            font_setting = "font='sans-serif'"
            print(f"Font file not found, using default: {font_file}")

        filter_str = (
            f"drawtext={font_setting}"
            f":text='{escaped_text}'"
            f":fontsize={style.size}"
            f":fontcolor={style.color}"
            f":borderw={style.outline_width}"
            f":bordercolor={style.outline_color}"
            f":x=(w-text_w)/2"
            f":y={y_pos}"
            f":enable='between(t,{caption.start_time},{caption.end_time})'"
            f"{shadow_settings}"
        )
        filters.append(filter_str)

    return ",".join(filters)

# === Core Functions ===

def create_video_from_image(image_path: str, audio_path: str, output_path: str) -> bool:
    try:
        cmd = ["ffmpeg", "-y", "-loop", "1", "-i", image_path, "-i", audio_path, "-c:v", "libx264", "-tune", "stillimage", "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p", "-shortest", output_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return result.returncode == 0
    except Exception as e:
        print(f"FFmpeg error: {e}")
        return False

def combine_video_audio(video_path: str, audio_path: str, output_path: str) -> bool:
    """動画と音声を合成（元の音声を保持してBGMをミックス）"""
    try:
        probe_cmd = [
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=index", "-of", "csv=p=0", video_path
        ]
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
        has_audio = bool(probe_result.stdout.strip())

        print(f"Video has audio track: {has_audio}")

        if has_audio:
            cmd = [
                "ffmpeg", "-y",
                "-i", video_path,
                "-i", audio_path,
                "-filter_complex",
                "[0:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,volume=1.0[voice];"
                "[1:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,volume=0.25[bgm];"
                "[voice][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]",
                "-map", "0:v",
                "-map", "[aout]",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-shortest",
                output_path
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-i", video_path,
                "-i", audio_path,
                "-map", "0:v",
                "-map", "1:a",
                "-c:v", "copy",
                "-c:a", "aac",
                "-shortest",
                output_path
            ]

        print(f"Running FFmpeg command: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if result.returncode != 0:
            print(f"FFmpeg error: {result.stderr}")
            return False

        return True
    except subprocess.TimeoutExpired:
        print("FFmpeg timeout")
        return False
    except Exception as e:
        print(f"FFmpeg error: {e}")
        return False

def add_captions_to_video(video_path: str, output_path: str, captions: list[Caption], styles: CaptionStyles) -> bool:
    """動画にテロップを追加"""
    try:
        drawtext_filter = build_drawtext_filter(captions, styles)

        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-vf", drawtext_filter,
            "-c:a", "copy",
            output_path
        ]

        print(f"Running FFmpeg caption command: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if result.returncode != 0:
            print(f"FFmpeg caption error: {result.stderr}")
            return False

        return True
    except Exception as e:
        print(f"FFmpeg caption error: {e}")
        return False

def combine_video_voice_bgm_captions(
    video_path: str,
    voice_path: str | None,
    bgm_path: str | None,
    output_path: str,
    captions: list[Caption],
    styles: CaptionStyles
) -> bool:
    """動画 + 音声(voice) + BGM + テロップを全て合成"""
    try:
        # テロップフィルタを構築
        drawtext_filter = build_drawtext_filter(captions, styles) if captions else ""
        has_captions = bool(drawtext_filter)
        has_voice = voice_path and os.path.exists(voice_path)
        has_bgm = bgm_path and os.path.exists(bgm_path)

        print(f"Processing: captions={has_captions}, voice={has_voice}, bgm={has_bgm}")

        # 入力ファイルリスト
        inputs = ["-i", video_path]
        input_count = 1
        voice_idx = None
        bgm_idx = None

        if has_voice:
            inputs.extend(["-i", voice_path])
            voice_idx = input_count
            input_count += 1

        if has_bgm:
            inputs.extend(["-i", bgm_path])
            bgm_idx = input_count
            input_count += 1

        # フィルタ構築
        filter_parts = []
        video_out = "0:v"
        audio_out = None

        # テロップフィルタ
        if has_captions:
            filter_parts.append(f"[0:v]{drawtext_filter}[vout]")
            video_out = "[vout]"

        # 音声フィルタ
        if has_voice and has_bgm:
            # 音声 + BGM をミックス
            filter_parts.append(
                f"[{voice_idx}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,volume=1.0[voice];"
                f"[{bgm_idx}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,volume=0.25[bgm];"
                f"[voice][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]"
            )
            audio_out = "[aout]"
        elif has_voice:
            # 音声のみ
            audio_out = f"{voice_idx}:a"
        elif has_bgm:
            # BGMのみ
            audio_out = f"{bgm_idx}:a"

        # コマンド構築
        cmd = ["ffmpeg", "-y"] + inputs

        if filter_parts:
            cmd.extend(["-filter_complex", ";".join(filter_parts)])

        # マッピング
        if has_captions:
            cmd.extend(["-map", video_out])
        else:
            cmd.extend(["-map", "0:v"])

        if audio_out:
            cmd.extend(["-map", audio_out])

        # エンコード設定
        if has_captions:
            cmd.extend(["-c:v", "libx264"])
        else:
            cmd.extend(["-c:v", "copy"])

        if audio_out:
            cmd.extend(["-c:a", "aac", "-b:a", "192k"])

        cmd.extend(["-shortest", output_path])

        print(f"Running FFmpeg command: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if result.returncode != 0:
            print(f"FFmpeg error: {result.stderr}")
            return False

        return True
    except Exception as e:
        print(f"FFmpeg error: {e}")
        return False

# === API Endpoints ===

@app.get("/")
async def health_check():
    return {"status": "ok", "ffmpeg_version": get_ffmpeg_version()}

@app.post("/combine", response_model=CombineResponse)
async def combine_video_and_audio(request: CombineRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())[:8]
    video_path = os.path.join(TEMP_DIR, f"{job_id}_video.mp4")
    audio_path = os.path.join(TEMP_DIR, f"{job_id}_audio.mp3")
    output_path = os.path.join(OUTPUT_DIR, f"{job_id}_output.{request.output_format}")
    try:
        if not await download_file(request.video_url, video_path):
            raise HTTPException(status_code=400, detail="Failed to download video")
        if not await download_file(request.audio_url, audio_path):
            raise HTTPException(status_code=400, detail="Failed to download audio")
        if not combine_video_audio(video_path, audio_path, output_path):
            raise HTTPException(status_code=500, detail="FFmpeg processing failed")
        background_tasks.add_task(lambda: os.remove(video_path) if os.path.exists(video_path) else None)
        background_tasks.add_task(lambda: os.remove(audio_path) if os.path.exists(audio_path) else None)
        return CombineResponse(success=True, job_id=job_id, message="Video combined", output_url=f"/download/{job_id}_output.{request.output_format}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/combine-with-captions", response_model=CombineResponse)
async def combine_with_captions(request: CombineWithCaptionsRequest, background_tasks: BackgroundTasks):
    """動画 + 音声(voice) + BGM + テロップを合成"""
    job_id = str(uuid.uuid4())[:8]
    video_path = os.path.join(TEMP_DIR, f"{job_id}_video.mp4")
    voice_path = os.path.join(TEMP_DIR, f"{job_id}_voice.mp3") if request.voice_url else None
    bgm_path = os.path.join(TEMP_DIR, f"{job_id}_bgm.mp3") if request.audio_url else None
    output_path = os.path.join(OUTPUT_DIR, f"{job_id}_output.{request.output_format}")

    print(f"=== combine-with-captions request ===")
    print(f"video_url: {request.video_url}")
    print(f"voice_url: {request.voice_url}")
    print(f"audio_url (BGM): {request.audio_url}")
    print(f"captions count: {len(request.captions) if request.captions else 0}")
    for i, cap in enumerate(request.captions or []):
        cap_preview = cap.text[:30] if len(cap.text) > 30 else cap.text
        print(f"  caption[{i}]: text='{cap_preview}...' start={cap.start_time} end={cap.end_time} pos={cap.position}")

    try:
        if not await download_file(request.video_url, video_path):
            raise HTTPException(status_code=400, detail="Failed to download video")
        print(f"Video downloaded: {os.path.exists(video_path)}, size: {os.path.getsize(video_path) if os.path.exists(video_path) else 0}")

        if request.voice_url and voice_path:
            if not await download_file(request.voice_url, voice_path):
                raise HTTPException(status_code=400, detail="Failed to download voice audio")
            print(f"Voice downloaded: {os.path.exists(voice_path)}, size: {os.path.getsize(voice_path) if os.path.exists(voice_path) else 0}")

        if request.audio_url and bgm_path:
            if not await download_file(request.audio_url, bgm_path):
                raise HTTPException(status_code=400, detail="Failed to download BGM audio")
            print(f"BGM downloaded: {os.path.exists(bgm_path)}, size: {os.path.getsize(bgm_path) if os.path.exists(bgm_path) else 0}")

        if not combine_video_voice_bgm_captions(
            video_path,
            voice_path,
            bgm_path,
            output_path,
            request.captions,
            request.caption_styles
        ):
            raise HTTPException(status_code=500, detail="FFmpeg processing failed")

        background_tasks.add_task(lambda: os.remove(video_path) if os.path.exists(video_path) else None)
        if voice_path:
            background_tasks.add_task(lambda: os.remove(voice_path) if os.path.exists(voice_path) else None)
        if bgm_path:
            background_tasks.add_task(lambda: os.remove(bgm_path) if os.path.exists(bgm_path) else None)

        return CombineResponse(
            success=True,
            job_id=job_id,
            message="Video with captions created",
            output_url=f"/download/{job_id}_output.{request.output_format}"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/image-to-video", response_model=CombineResponse)
async def image_to_video(request: ImageToVideoRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())[:8]
    image_path = os.path.join(TEMP_DIR, f"{job_id}_image.png")
    audio_path = os.path.join(TEMP_DIR, f"{job_id}_audio.mp3")
    output_path = os.path.join(OUTPUT_DIR, f"{job_id}_output.{request.output_format}")
    try:
        if not await download_file(request.image_url, image_path):
            raise HTTPException(status_code=400, detail="Failed to download image")
        if not await download_file(request.audio_url, audio_path):
            raise HTTPException(status_code=400, detail="Failed to download audio")
        if not create_video_from_image(image_path, audio_path, output_path):
            raise HTTPException(status_code=500, detail="FFmpeg processing failed")
        background_tasks.add_task(lambda: os.remove(image_path) if os.path.exists(image_path) else None)
        background_tasks.add_task(lambda: os.remove(audio_path) if os.path.exists(audio_path) else None)
        return CombineResponse(success=True, job_id=job_id, message="Video created", output_url=f"/download/{job_id}_output.{request.output_format}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/download/{filename}")
async def download_file_endpoint(filename: str):
    filepath = os.path.join(OUTPUT_DIR, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(filepath, media_type="video/mp4", filename=filename)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
