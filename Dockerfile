FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy project files needed for install
COPY pyproject.toml ./
COPY chuddy/ ./chuddy/

# Install dependencies (editable install so `python -m chuddy` works)
RUN pip install --no-cache-dir -e .

# Copy runtime data files
COPY config.yml ./
COPY cookies/ ./cookies/

RUN mkdir -p /data /tmp/download_tmpfs /filestorage

ENV DATA_DIR=/data
ENV TMP_DOWNLOAD_ROOT_PATH=/tmp/download_tmpfs
ENV STORAGE_PATH=/filestorage
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

CMD ["python", "-m", "chuddy"]
