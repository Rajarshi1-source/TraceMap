# Node.js 24 LTS. NOT node:20 - end of life April 2026 (M18).
FROM node:24-alpine AS deps
WORKDIR /app
COPY package*.json ./
RUN npm install --omit=dev --no-audit --no-fund

FROM node:24-alpine AS runtime
WORKDIR /app
RUN addgroup -S app && adduser -S app -G app
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
