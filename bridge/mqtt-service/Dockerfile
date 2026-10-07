FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
# --progress-bar off: pip's default rich-based progress bar spawns a
# background refresh thread, which fails with "can't start new thread" on
# resource-constrained boards (e.g. a Jetson low on RAM/process headroom).
RUN pip install --no-cache-dir --progress-bar off -r requirements.txt

COPY app.py init.sql ./

EXPOSE 8001

CMD ["python", "app.py"]
