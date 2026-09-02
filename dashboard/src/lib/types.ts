import { z } from "zod";

/**
 * The data contract, validated at the boundary.
 *
 * A schema change in the analytics API should fail loudly in one place, not
 * produce `NaN` widths deep inside a D3 attribute setter where the symptom is
 * an invisible edge and the cause is three layers away.
 */
export const EdgeSchema = z.object({
  source: z.string(),
  target: z.string(),
  requestCount: z.number(),
  errorRate: z.number(),
  wallP50: z.number(),
  wallP95: z.number(),
  wallP99: z.number(),
  critP99: z.number(),
  critMeanMs: z.number(),
  /** 0..1 share of total user-visible latency. Drives opacity in critical mode. */
  blameShare: z.number(),
});

export const NodeSchema = z.object({
  service: z.string(),
  requestCount: z.number(),
  errorRate: z.number(),
  p99: z.number(),
  deployedRecently: z.boolean(),
});

export const GraphSchema = z.object({
  nodes: z.array(NodeSchema),
  edges: z.array(EdgeSchema),
  computedAt: z.number().nullable().optional(),
  ageSeconds: z.number().optional(),
  /** True when the cache is serving last-good data. Drives the banner. */
  stale: z.boolean().optional(),
  source: z.string().optional(),
  windowMinutes: z.number().optional(),
});

export type Edge = z.infer<typeof EdgeSchema>;
export type ServiceNode = z.infer<typeof NodeSchema>;
export type Graph = z.infer<typeof GraphSchema>;

/** Wall clock is what every other service map shows. Critical path is the point. */
export type MetricMode = "wall" | "critical";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8090";
