FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system deps required by opencv and linux video
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       libglib2.0-0 v4l-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py ./

# Run as root to allow access to /dev/video0 by default. If you prefer non-root,
# drop privileges and ensure the container user has permission to access the device.
CMD ["python", "bot.py"]
