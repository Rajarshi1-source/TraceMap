# Python 3.14 (M18 - pinned, current).
FROM python:3.14-slim AS runtime
WORKDIR /app

RUN groupadd -r app && useradd -r -g app app

COPY requirements.txt ./
# setuptools is upgraded before anything else: the base image ships 70.3.0,
# which carries CVE-2025-47273 (path traversal). It is not in requirements.txt
# because nothing here imports it -- it arrives with the interpreter, and an
# image inherits its base's CVEs whether or not it uses the package.
#
# pip is then REMOVED, for the same reason npm is removed from the Node images:
# it is a build tool in a runtime image, and its VENDORED dependencies carry
# CVEs the application never imports. msgpack 1.1.2 (GHSA-6v7p-g79w-8964) lives
# at pip/_vendor/msgpack and is not installed as a package at all, so no
# requirements pin could ever have fixed it. This service runs uvicorn.
RUN pip install --no-cache-dir --upgrade "setuptools>=78.1.1"  && pip install --no-cache-dir -r requirements.txt  && pip uninstall -y pip setuptools >/dev/null 2>&1 || true  && rm -rf /usr/local/lib/python3.14/site-packages/pip            /usr/local/lib/python3.14/site-packages/pip-*            /usr/local/lib/python3.14/site-packages/setuptools            /usr/local/lib/python3.14/site-packages/setuptools-*            /usr/local/lib/python3.14/site-packages/pkg_resources            /usr/local/lib/python3.14/ensurepip
COPY --chown=app:app . .
USER app

# `opentelemetry-bootstrap` would add instrumentation packages at build time;
# they are pinned in requirements.txt instead so the image is reproducible.
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz',timeout=2).status==200 else 1)"
# Zero-code: the agent wraps the process. Go cannot do this (M11).
CMD ["opentelemetry-instrument", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
