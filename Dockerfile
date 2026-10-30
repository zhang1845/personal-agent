FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/code
WORKDIR /app
COPY requirements.lock.txt requirements-cloud.txt ./
RUN python -m pip install --no-cache-dir -r requirements-cloud.txt \
    && useradd --uid 10001 --create-home daynest \
    && mkdir /data && chown daynest:daynest /data
# An explicit source allowlist: no output/, private/, .env or local API settings.
COPY code/ ./code/
COPY assets/web/ ./assets/web/
USER daynest
ENV DAYNEST_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/healthz',timeout=3)"
CMD ["python", "-X", "utf8", "-B", "code/run_cloud.py", "--host", "0.0.0.0"]
