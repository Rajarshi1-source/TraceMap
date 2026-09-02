# Python 3.14 (M18).
FROM python:3.14-slim AS runtime
WORKDIR /app

RUN groupadd -r app && useradd -r -g app app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app . .
USER app

# /app is the import root, so packages are `src.*` and `workers.*`. There is
# deliberately no top-level `platform` package: it would shadow the stdlib
# module of the same name and break anything calling
# platform.python_implementation().
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1

EXPOSE 9100
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9100/metrics',timeout=2).status==200 else 1)"
CMD ["python", "-m", "workers.trace_assembler_worker"]
