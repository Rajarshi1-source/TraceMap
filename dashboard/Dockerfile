# Node.js 24 LTS (M18). Next.js 16.3 with standalone output.
FROM node:24-alpine AS deps
WORKDIR /app
COPY package.json ./
RUN npm install --no-audit --no-fund

FROM node:24-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:24-alpine AS runtime
WORKDIR /app
RUN addgroup -S app && adduser -S app -G app
# HOSTNAME=0.0.0.0 is NOT optional. Next's standalone server.js binds to
# process.env.HOSTNAME, and Docker sets HOSTNAME to the container ID - so the
# server ends up bound to the container's own hostname rather than all
# interfaces. The published port still works from outside, which makes this
# look fine; anything connecting to 127.0.0.1 inside the container, such as the
# healthcheck, gets ECONNREFUSED.
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3002 HOSTNAME=0.0.0.0
# standalone ships the server plus only the dependencies actually traced,
# rather than the whole node_modules tree.
COPY --from=build --chown=app:app /app/.next/standalone ./
COPY --from=build --chown=app:app /app/.next/static ./.next/static
USER app
EXPOSE 3002
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
  CMD node -e "require('http').get('http://127.0.0.1:3002/',r=>process.exit(r.statusCode<500?0:1)).on('error',()=>process.exit(1))"
CMD ["node", "server.js"]
