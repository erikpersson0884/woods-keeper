FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    curl \
    jq \
    docker.io \
    util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir flask python-dotenv

COPY app.py config.py storage.py stack-updater.sh favicon.png /app/
COPY templates /app/templates
COPY static /app/static
RUN mkdir -p /app/data
RUN chmod +x /app/stack-updater.sh

EXPOSE 5000

CMD ["python", "app.py"]
