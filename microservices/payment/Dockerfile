# Python 3.14 (M18 - pinned, current).
FROM python:3.14-slim AS runtime
WORKDIR /app

RUN groupadd -r app && useradd -r -g app app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app . .
USER app

# `opentelemetry-bootstrap` would add instrumentation packages at build time;
# they are pinned in requirements.txt instead so the image is reproducible.
EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8001/healthz',timeout=2).status==200 else 1)"
# Zero-code: the agent wraps the process. Go cannot do this (M11).
CMD ["opentelemetry-instrument", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
