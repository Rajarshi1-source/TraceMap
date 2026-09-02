import { buildApp } from './app.js';
import { close } from './db.js';

const PORT = Number(process.env.PORT ?? 3001);
const SERVICE = process.env.SERVICE_NAME ?? 'order-svc';

const server = buildApp().listen(PORT, () => {
  console.log(
    JSON.stringify({
      level: 'info',
      service: SERVICE,
      version: process.env.SERVICE_VERSION ?? 'dev-local',
      msg: `listening on :${PORT}`,
    }),
  );
});

// Graceful shutdown: stop accepting connections, drain in-flight requests, then
// release the pool. Without this a rolling update drops live checkouts.
for (const sig of ['SIGTERM', 'SIGINT']) {
  process.on(sig, () => {
    console.log(JSON.stringify({ level: 'info', service: SERVICE, msg: `${sig} received, draining` }));
    server.close(async () => {
      await close().catch(() => {});
      process.exit(0);
    });
    setTimeout(() => process.exit(1), 10_000).unref();
  });
}
