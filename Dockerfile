FROM python:3.12-slim
WORKDIR /srv
# ffmpeg provides ffprobe, used by the slow picker to measure true video bitrate.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && groupadd --gid 1000 stream-picker \
    && useradd --uid 1000 --gid stream-picker --no-create-home --shell /usr/sbin/nologin stream-picker \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
EXPOSE 8000
STOPSIGNAL SIGTERM
# The bootstrap repairs ownership on fresh/legacy bind mounts, then drops to
# UID/GID 1000 before importing or starting the application.
ENTRYPOINT ["python", "-m", "app.entrypoint"]
# The app's middleware emits a masked access line. Uvicorn's duplicate access
# logger would print the raw addon-secret path, so keep it disabled. Forwarded
# headers are resolved by app.adminui against TRUSTED_PROXIES; Uvicorn must keep
# the socket peer intact for that check.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers", "--timeout-graceful-shutdown", "40"]
