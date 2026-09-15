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

# Data directory + dedicated non-root service account
RUN mkdir -p /opt/safescanarr/data/vcs \
    && useradd --system --create-home --shell /usr/sbin/nologin safescanarr \
    && chown -R safescanarr:safescanarr /opt/safescanarr

USER safescanarr

EXPOSE 8686

# Unauthenticated liveness probe; /health reveals nothing beyond "ok".
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8686/health', timeout=3).status == 200 else 1)"

CMD ["python", "entrypoint.py"]
