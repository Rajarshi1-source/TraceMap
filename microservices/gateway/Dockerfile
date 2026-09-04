# Node.js 24 LTS. NOT node:20 - end of life April 2026 (M18).
FROM node:24-alpine AS deps
WORKDIR /app
COPY package*.json ./
# `npm ci`, not `npm install`. install resolves within semver ranges and will
# happily pull a NEWER transitive dependency than the lockfile pins -- which is
# how this image shipped brace-expansion 5.0.7, ip-address 10.2.0 and tar
# 7.5.19 while the committed lockfile held patched versions of all three. ci
# installs the lockfile exactly, so the image and CI agree on what was audited.
RUN npm ci --omit=dev --no-audit --no-fund

FROM node:24-alpine AS runtime
WORKDIR /app
# Pick up OS security fixes published after the base image was cut. Trivy
# flagged libcrypto3 3.5.7-r0 (CVE-2026-14456) against a base tag that had not
# been rebuilt yet; pinning the base and never upgrading means the image's CVE
# count only ever grows between tag bumps.
# npm is a BUILD tool and this is a runtime image. Removing it drops three
# HIGH CVEs that live in npm's own bundled dependencies -- brace-expansion,
# ip-address and tar under /usr/local/lib/node_modules/npm -- none of which
# this service ever imports. They survived the switch to `npm ci` precisely
# because they were never in the application's dependency tree; they ship with
# the base image. `node server.js` needs the runtime, not the package manager.
RUN apk upgrade --no-cache  && rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx  && addgroup -S app && adduser -S app -G app
COPY --from=deps --chown=app:app /app/node_modules ./node_modules
COPY --chown=app:app package.json ./
COPY --chown=app:app src ./src
USER app

# Zero-code instrumentation. Node and Python get this genuinely, via a runtime
# agent; Go does NOT (M11) - see microservices/product, which wraps handlers
# explicitly because a compiled binary has no runtime hook to attach to.
ENV NODE_OPTIONS="--require @opentelemetry/auto-instrumentations-node/register"

EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD node -e "require('http').get('http://localhost:3000/healthz',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"
CMD ["node", "src/server.js"]
