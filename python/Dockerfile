# Dockerfile for Talestral FastAPI Transcript Sink
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

EXPOSE 8765

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8765/health || exit 1

CMD ["uv", "run", "uvicorn", "transcript_sink:app", "--host", "0.0.0.0", "--port", "8765"]
