# Coordination server. Stdlib only, so a slim base with no pip install.
FROM python:3.11-slim

WORKDIR /app
COPY server/ /app/server/

# Persist the SQLite claim store on a volume so restarts don't drop live claims.
ENV COORD_HOST=0.0.0.0 \
    COORD_PORT=8787 \
    COORD_DB=/data/coordinator.db \
    COORD_TTL_SECONDS=900
VOLUME ["/data"]
EXPOSE 8787

# COORD_TOKEN should be provided at run time:
#   docker run -e COORD_TOKEN=secret -p 8787:8787 -v coord-data:/data <image>
# Container liveness via the auth-free health endpoint.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/healthz',timeout=2).status==200 else 1)"

CMD ["python3", "server/coordinator.py"]
