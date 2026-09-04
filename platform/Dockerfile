# Python 3.14 (M18).
FROM python:3.14-slim AS runtime
WORKDIR /app

RUN groupadd -r app && useradd -r -g app app

COPY requirements.txt ./
# Same treatment as the microservices: setuptools upgraded (the interpreter
# ships 70.3.0, CVE-2025-47273), then pip removed. pip is a build tool, and its
# VENDORED dependencies carry CVEs nothing here imports -- msgpack 1.1.2 lives
# at pip/_vendor/msgpack and is not an installed package, so no requirements
# pin could reach it. These workers run `python -m workers.*`.
RUN pip install --no-cache-dir --upgrade "setuptools>=78.1.1"  && pip install --no-cache-dir -r requirements.txt  && pip uninstall -y pip setuptools >/dev/null 2>&1 || true  && rm -rf /usr/local/lib/python3.14/site-packages/pip            /usr/local/lib/python3.14/site-packages/pip-*            /usr/local/lib/python3.14/site-packages/setuptools            /usr/local/lib/python3.14/site-packages/setuptools-*            /usr/local/lib/python3.14/site-packages/pkg_resources            /usr/local/lib/python3.14/ensurepip

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
