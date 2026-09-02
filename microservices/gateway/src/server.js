import { buildApp } from './app.js';

const PORT = Number(process.env.PORT ?? 3000);
const SERVICE = process.env.SERVICE_NAME ?? 'gateway-svc';

const server = buildApp().listen(PORT, () => {
  console.log(JSON.stringify({
    level: 'info',
    service: SERVICE,
    version: process.env.SERVICE_VERSION ?? 'dev-local',
    msg: `listening on :${PORT}`,
  }));
});

for (const sig of ['SIGTERM', 'SIGINT']) {
  process.on(sig, () => {
    console.log(JSON.stringify({ level: 'info', service: SERVICE, msg: `${sig} received, draining` }));
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(1), 10_000).unref();
  });
}
