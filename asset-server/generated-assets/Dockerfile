FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
COPY serve_release.py /app/serve_release.py
COPY sync_release.py /app/sync_release.py
CMD ["python", "/app/serve_release.py", "serve", "--root", "/data", "--bind", "127.0.0.1", "--port", "8765"]
