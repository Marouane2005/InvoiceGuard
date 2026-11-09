FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 INVOICEGUARD_DATA_DIR=/data
WORKDIR /app
COPY pyproject.toml README.md constraints.txt ./
COPY invoiceguard ./invoiceguard
RUN pip install --no-cache-dir -c constraints.txt . \
    && useradd --system --uid 10001 --no-create-home app \
    && mkdir /data /backups && chown app /data /backups
# Image reading in INVOICEGUARD_READER=local shells out to tesseract. The base image is Debian,
# so an Ubuntu package pin would not install. Versions move with the image rebuild.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*
USER 10001
VOLUME ["/data", "/backups"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=3)"]
CMD ["invoiceguard", "serve", "--host", "0.0.0.0", "--port", "8000"]
