FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
COPY assets_route.py /app/assets_route.py
COPY serve_release.py /app/serve_release.py
COPY scripts /app/scripts
CMD ["python", "/app/assets_route.py", "serve", "--root", "/data", "--bind", "127.0.0.1", "--port", "8765"]
COPY sync_loop.py /app/sync_loop.py
COPY sync_release.py /app/sync_release.py
