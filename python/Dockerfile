# Multi-stage Dockerfile for Talestral Python Services
# Supports both FastAPI and Streamlit deployments

FROM python:3.12-slim as base

# Install uv for fast package management
RUN pip install --no-cache-dir uv

WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies using uv
RUN uv sync --frozen --no-dev

# Copy application code
COPY . .

# ==============================================================================
# FastAPI Target (default)
# ==============================================================================
FROM base as fastapi

EXPOSE 8765

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8765/health || exit 1

CMD ["uv", "run", "uvicorn", "transcript_sink:app", "--host", "0.0.0.0", "--port", "8765"]

# ==============================================================================
# Streamlit Target
# ==============================================================================
FROM base as streamlit

EXPOSE 8501

# Streamlit configuration
ENV STREAMLIT_SERVER_PORT=8501
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0
ENV STREAMLIT_SERVER_HEADLESS=true
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

CMD ["uv", "run", "streamlit", "run", "streamlit_ui.py", "--server.port=8501", "--server.address=0.0.0.0"]
