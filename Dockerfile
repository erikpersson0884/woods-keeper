FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    curl \
    jq \
    docker.io \
    util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir flask python-dotenv

COPY app.py auto-update.sh favicon.png /app/
RUN chmod +x /app/auto-update.sh

EXPOSE 5000

CMD ["python", "app.py"]
