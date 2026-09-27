# Redi coordination server. Stdlib-only package, so no third-party deps to pull.
FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY redi/ ./redi/
RUN pip install --no-cache-dir .

# Persist the SQLite store AND the auto-generated token on a volume (spec C3).
ENV COORD_HOST=0.0.0.0 \
    COORD_PORT=8787 \
    COORD_DB=/data/redi.db \
    COORD_DATA_DIR=/data \
    COORD_TTL_SECONDS=900
VOLUME ["/data"]
EXPOSE 8787

# Container liveness via the auth-free health endpoint.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/healthz',timeout=2).status==200 else 1)"

# Prints the join string on every startup; token auto-generated to /data.
CMD ["redi", "serve"]
