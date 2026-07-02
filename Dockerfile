FROM python:3.13-slim
WORKDIR /app
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
    curl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py *.json ./
COPY frontend/ ./frontend/
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -sf http://localhost:8000/health || exit 1
EXPOSE 8000
CMD ["python3", "auth_server_fastapi.py"]
