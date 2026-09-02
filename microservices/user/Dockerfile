# Python 3.14 (M18 - pinned, current).
FROM python:3.14-slim AS runtime
WORKDIR /app

RUN groupadd -r app && useradd -r -g app app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app . .
USER app

# Phase 2 wraps the entrypoint with the auto-instrumentation agent:
#   opentelemetry-instrument --service_name user-svc uvicorn main:app ...

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz',timeout=2).status==200 else 1)"
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
