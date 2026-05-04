FROM python:3.11-slim

# Install ffmpeg (required by vcsi)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/safescanarr

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . .

# Data directories (override via volumes in docker-compose)
RUN mkdir -p /opt/safescanarr/data/vcs

EXPOSE 8686

CMD ["python", "entrypoint.py"]
