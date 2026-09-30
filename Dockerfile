FROM python:3.12-slim

# ping for ICMP checks
RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY agent ./agent

RUN useradd --system --uid 1000 --home /data monitor \
    && mkdir -p /data && chown monitor /data
USER monitor

ENV MONITOR_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)"

# Exactly one worker: the check scheduler runs inside the web process.
# Behind a reverse proxy, set FORWARDED_ALLOW_IPS to the proxy's address so client IPs are trusted
# from it only (uvicorn reads this variable; default 127.0.0.1).
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
