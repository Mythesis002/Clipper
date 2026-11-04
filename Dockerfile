FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-noto-core ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements-scheduler.txt ./
RUN pip install -r requirements.txt -r requirements-scheduler.txt
COPY . .
CMD ["python","-m","scheduler.worker"]
