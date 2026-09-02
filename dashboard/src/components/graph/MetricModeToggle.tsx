"use client";

import type { MetricMode } from "@/lib/types";

/**
 * The four seconds that carry the entire product thesis.
 *
 * In wall-clock mode two edges look equally bad. Flip to critical path and one
 * fades to near-transparent while the other stays solid. The line that goes
 * with it:
 *
 *   "that edge has a 47ms p99 and contributes zero milliseconds of user-visible
 *    latency, because it runs in parallel. Every service map I've used would
 *    have sent me to debug it."
 *
 * The transition is animated on purpose. An instant re-render reads as a page
 * change; a 400ms ease reads as the same graph being re-judged, which is what
 * actually happened.
 */
export default function MetricModeToggle({
  mode,
  onChange,
}: {
  mode: MetricMode;
  onChange: (m: MetricMode) => void;
}) {
  return (
    <div className="toggle" role="group" aria-label="Latency metric">
      <button
        type="button"
        className={mode === "wall" ? "toggle-btn is-active" : "toggle-btn"}
        aria-pressed={mode === "wall"}
        onClick={() => onChange("wall")}
      >
        Wall clock
      </button>
      <button
        type="button"
        className={mode === "critical" ? "toggle-btn is-active" : "toggle-btn"}
        aria-pressed={mode === "critical"}
        onClick={() => onChange("critical")}
      >
        Critical path
      </button>
    </div>
  );
}
