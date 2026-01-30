import os
import uuid
import subprocess
import tempfile
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
os.makedirs(OUTPUT_DIR, exist_ok=True)

class CombineRequest(BaseModel):
    video_url: str
    audio_url: str
    output_format: str = "mp4"

class ImageToVideoRequest(BaseModel):
    image_url: str
    audio_url: str
    output_format: str = "mp4"

class CombineResponse(BaseModel):
    success: bool
    job_id: str
    message: str
    output_url: str | None = None

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
        # まず動画に音声トラックがあるか確認
        probe_cmd = [
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=index", "-of", "csv=p=0", video_path
        ]
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
        has_audio = bool(probe_result.stdout.strip())

        print(f"Video has audio track: {has_audio}")

        if has_audio:
            # 元の動画に音声がある場合: ミックス
            cmd = [
                "ffmpeg",
                "-y",
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
            # 音声トラックがない場合: BGMのみ追加
            cmd = [
                "ffmpeg",
                "-y",
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
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300
        )

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
