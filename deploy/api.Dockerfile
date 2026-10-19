FROM docker:27-cli AS docker
FROM python:3.11-slim-bookworm
COPY --from=docker /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /opt/city
COPY pyproject.toml README.md alembic.ini ./
COPY migrations/ ./migrations/
COPY tsinghua_city/ ./tsinghua_city/
RUN pip install --no-cache-dir '.[postgres]'
ENV PYTHONPATH=/opt/city CITY_HOST=0.0.0.0 CITY_RUNTIME_LOG_DIR=/var/lib/city/runtime \
    CITY_TRACE_DIR=/var/lib/city/transcripts CITY_MANIFEST_DIR=/var/lib/city/manifests
CMD ["sh", "-c", "tsinghua-city upgrade && exec tsinghua-city serve"]
