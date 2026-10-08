FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends fonts-lato && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY renderer ./renderer
COPY config.example.json .
USER 65534:65534
CMD ["python", "-m", "renderer.app", "--config", "/app/config.json", "--host", "0.0.0.0"]
