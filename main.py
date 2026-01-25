import os
import uuid
import subprocess
import tempfile
import shutil
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import httpx
import asyncio

app = FastAPI(title="FFmpeg Video Combiner API")

# CORS設定
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 一時ファイル保存用ディレクトリ
TEMP_DIR = tempfile.gettempdir()
OUTPUT_DIR = os.path.join(TEMP_DIR, "ffmpeg_output")
os.makedirs(OUTPUT_DIR, exist_ok=True)


class CombineRequest(BaseModel):
    video_url: str
    audio_url: str
    output_format: str = "mp4"


class CombineResponse(BaseModel):
    success: bool
    job_id: str
    message: str
    output_url: str | None = None


class HealthResponse(BaseModel):
    status: str
    ffmpeg_version: str


async def download_file(url: str, dest_path: str) -> bool:
    """URLからファイルをダウンロード"""
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
    """FFmpegのバージョンを取得"""
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True
        )
        first_line = result.stdout.split('\n')[0]
        return first_line
    except Exception:
        return "FFmpeg not found"


def combine_video_audio(video_path: str, audio_path: str, output_path: str) -> bool:
    """動画と音声を合成"""
    try:
        # FFmpegコマンド: 動画に音声を追加（既存の音声を置き換え）
        cmd = [
            "ffmpeg",
            "-y",  # 上書き許可
            "-i", video_path,  # 入力動画
            "-i", audio_path,  # 入力音声（BGM）
            "-filter_complex",
            # 元の動画の音声とBGMをミックス（BGMは音量を下げる）
            "[0:a]volume=1.0[a0];[1:a]volume=0.3[a1];[a0][a1]amix=inputs=2:duration=first[aout]",
            "-map", "0:v",  # 動画は元のまま
            "-map", "[aout]",  # 音声はミックスしたもの
            "-c:v", "copy",  # 動画はコピー（再エンコードなし）
            "-c:a", "aac",  # 音声はAAC
            "-shortest",  # 短い方に合わせる
            output_path
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300  # 5分タイムアウト
        )

        if result.returncode != 0:
            print(f"FFmpeg error: {result.stderr}")
            # 音声トラックがない場合のフォールバック
            cmd_fallback = [
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
            result = subprocess.run(
                cmd_fallback,
                capture_output=True,
                text=True,
                timeout=300
            )

        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print("FFmpeg timeout")
        return False
    except Exception as e:
        print(f"FFmpeg error: {e}")
        return False


def cleanup_old_files():
    """古い一時ファイルを削除"""
    import time
    current_time = time.time()
    for filename in os.listdir(OUTPUT_DIR):
        filepath = os.path.join(OUTPUT_DIR, filename)
        if os.path.isfile(filepath):
            file_age = current_time - os.path.getmtime(filepath)
            if file_age > 3600:  # 1時間以上前のファイルを削除
                os.remove(filepath)


@app.get("/", response_model=HealthResponse)
async def health_check():
    """ヘルスチェック"""
    return HealthResponse(
        status="ok",
        ffmpeg_version=get_ffmpeg_version()
    )


@app.post("/health")
async def health_check_post():
    """n8nテスト用ヘルスチェック（POST）"""
    return {"success": True, "message": "FFmpeg server is running"}


@app.post("/combine", response_model=CombineResponse)
async def combine_video_and_audio(
    request: CombineRequest,
    background_tasks: BackgroundTasks
):
    """動画と音声を合成するエンドポイント"""
    job_id = str(uuid.uuid4())[:8]

    # 一時ファイルパス
    video_path = os.path.join(TEMP_DIR, f"{job_id}_video.mp4")
    audio_path = os.path.join(TEMP_DIR, f"{job_id}_audio.mp3")
    output_path = os.path.join(OUTPUT_DIR, f"{job_id}_output.{request.output_format}")

    try:
        # ファイルをダウンロード
        video_downloaded = await download_file(request.video_url, video_path)
        if not video_downloaded:
            raise HTTPException(status_code=400, detail="Failed to download video")

        audio_downloaded = await download_file(request.audio_url, audio_path)
        if not audio_downloaded:
            raise HTTPException(status_code=400, detail="Failed to download audio")

        # FFmpegで合成
        success = combine_video_audio(video_path, audio_path, output_path)

        if not success:
            raise HTTPException(status_code=500, detail="FFmpeg processing failed")

        # 一時ファイルを削除（バックグラウンド）
        background_tasks.add_task(lambda: os.remove(video_path) if os.path.exists(video_path) else None)
        background_tasks.add_task(lambda: os.remove(audio_path) if os.path.exists(audio_path) else None)
        background_tasks.add_task(cleanup_old_files)

        # 出力ファイルのURLを返す
        return CombineResponse(
            success=True,
            job_id=job_id,
            message="Video combined successfully",
            output_url=f"/download/{job_id}_output.{request.output_format}"
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/download/{filename}")
async def download_file_endpoint(filename: str):
    """合成済み動画をダウンロード"""
    filepath = os.path.join(OUTPUT_DIR, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        filepath,
        media_type="video/mp4",
        filename=filename
    )


@app.post("/test")
async def test_endpoint():
    """n8n接続テスト用"""
    return {
        "success": True,
        "message": "Connection test successful",
        "ffmpeg_available": "ffmpeg" in get_ffmpeg_version().lower()
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
