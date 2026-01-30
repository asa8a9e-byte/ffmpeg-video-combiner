# FFmpeg + Python FastAPI
FROM python:3.11-slim

# FFmpegと日本語フォントをインストール
RUN apt-get update && apt-get install -y \
    ffmpeg \
    fonts-noto-cjk \
    fonts-noto-cjk-extra \
    fontconfig \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

# 作業ディレクトリ
WORKDIR /app

# 依存関係をインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションをコピー
COPY main.py .

# ポート設定（Railway/Renderは環境変数PORTを使用）
ENV PORT=8000
EXPOSE 8000

# 起動コマンド
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
