FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

ENV PORT=7860
EXPOSE 7860

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-7860} --workers 2 --timeout 300 app:app"]
