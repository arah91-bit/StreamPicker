FROM python:3.12-slim
WORKDIR /srv
# ffmpeg provides ffprobe, used by the slow picker to measure true video bitrate.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
# The app's middleware emits a masked access line. Uvicorn's duplicate access
# logger would print the raw addon-secret path, so keep it disabled.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
