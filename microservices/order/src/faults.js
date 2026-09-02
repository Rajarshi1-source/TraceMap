/**
 * Runtime-mutable fault state with a TTL backstop (M23).
 *
 * Faults are read PER REQUEST from mutable config. Never restart to apply a
 * fault: a restart perturbs latency on its own, so restarting to inject a
 * latency fault measures the restart.
 *
 * The TTL matters because the eval runner can crash mid-case. Without it, a
 * leaked fault silently poisons every subsequent measurement and you spend an
 * evening confused.
 */

const num = (v, d) => (v === undefined || v === '' ? d : Number(v));

export const NO_FAULT = Object.freeze({
  delayMs: 0,
  errorRate: 0,
  cpuBurnMs: 0,
});

class FaultConfig {
  #fault = NO_FAULT;
  #endpoint = '';
  #expiresAt = 0;

  constructor() {
    // Everything defaults to zero/off. A fault must never be on by accident.
    const delayMs = num(process.env.TRACEMAP_FAULT_DELAY_MS, 0);
    const errorRate = num(process.env.TRACEMAP_FAULT_ERROR_RATE, 0);
    const cpuBurnMs = num(process.env.TRACEMAP_FAULT_CPU_BURN_MS, 0);

    if (delayMs || errorRate || cpuBurnMs) {
      this.set(
        { delayMs, errorRate, cpuBurnMs },
        process.env.TRACEMAP_FAULT_ENDPOINT ?? '',
        num(process.env.TRACEMAP_FAULT_TTL_S, 600),
      );
    }
  }

  set(fault, endpoint = '', ttlS = 600) {
    this.#fault = Object.freeze({ ...NO_FAULT, ...fault });
    this.#endpoint = endpoint;
    this.#expiresAt = Date.now() + ttlS * 1000;
  }

  /** Idempotent - called from `finally`, and possibly twice. */
  clear() {
    this.#fault = NO_FAULT;
    this.#endpoint = '';
    this.#expiresAt = 0;
  }

  forEndpoint(path) {
    if (Date.now() > this.#expiresAt) return NO_FAULT; // expired: self-heals
    if (this.#endpoint && this.#endpoint !== path) return NO_FAULT; // scoped
    return this.#fault;
  }

  describe() {
    const active = Date.now() <= this.#expiresAt;
    return {
      active,
      endpoint: this.#endpoint || null,
      expiresInS: active ? Math.round((this.#expiresAt - Date.now()) / 1000) : 0,
      ...(active ? this.#fault : NO_FAULT),
    };
  }
}

export const faultConfig = new FaultConfig();

export const sleep = (ms) =>
  ms > 0 ? new Promise((r) => setTimeout(r, ms)) : Promise.resolve();
