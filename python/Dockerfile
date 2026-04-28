# Dockerfile for LegionMeet FastAPI Transcript Sink
# Default target: FastAPI service (transcript_sink.py)
#
# Build:  docker build -t talestral-api .
# Run:    docker run -p 8765:8765 talestral-api

FROM python:3.12-slim

# Install uv for fast package management
RUN pip install --no-cache-dir uv

WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies using uv
RUN uv sync --frozen --no-dev

# Copy application code
COPY . .

# ALFRED runtime defaults. transcript_sink.load_runtime_config() raises on
# empty PRODUCT_SPEC_PATH/VARIANT_ID, so the container needs these set or
# overridden at deploy time.
ENV VARIANT_ID=alfred \
    INSTANCE_ID=alfred \
    PRODUCT_SPEC_PATH=/app/legionmeet_platform/specs/alfred.yaml \
    SINK_HOST=0.0.0.0 \
    SINK_PORT=8765

EXPOSE 8765

# Health check. python:3.12-slim does not ship curl, so use stdlib instead.
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8765/health').status==200 else 1)" || exit 1

CMD ["uv", "run", "uvicorn", "transcript_sink:app", "--host", "0.0.0.0", "--port", "8765"]
