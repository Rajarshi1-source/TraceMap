---
name: tracemap-nextjs-dashboard
description: Build TraceMap's dashboard — Next.js 16.3 App Router with React 19 and TypeScript strict, the D3 v7 force-directed dependency map, the wall-clock versus critical-path metric toggle that is the product's demo moment, edge detail pages with deploy markers, the SSE alert feed, the detector scoreboard and cost pages, styled with Tailwind and shadcn/ui copied into the repo. Use for ANY frontend work in this project - pages under app/, server versus client components, the graph canvas, force simulation tuning, visual encoding of latency and error rate, polling and caching, loading and empty states, or dashboard styling - even when Next.js is not named. MANDATE - Next.js 15 reaches end of life 21 Oct 2026 so build on 16.3, never the Pages Router; D3 mutates the DOM so the graph lives in a client component; and node positions must persist across refreshes or the layout jumps every ten seconds and looks broken. Pair with tracemap-clickhouse-analytics for the data contract.
---

# TraceMap — Dependency Map Dashboard

The dashboard is the surface an interviewer actually sees. Everything else in the project is invisible until this renders.

## Stack

| Layer | Pin | Note |
|---|---|---|
| Framework | **Next.js 16.3** App Router | 15 goes EOL 21 Oct 2026 — do not start on it |
| React | **19** | React Compiler support is stable in 16 |
| Language | TypeScript strict | Zod-validated API responses at the boundary |
| Graph | **D3 v7** force simulation | Right tool for a custom force-directed graph |
| Styling | Tailwind + shadcn/ui | Components copied **into** the repo, never installed as an npm dependency |
| Runtime | Node.js 24 LTS | |

## The toggle that wins the demo

One control, top-right: **`Wall clock <-> Critical path`**.

Two edges look equally red in wall-clock mode. Flip to critical-path mode and one fades to near-transparent. The line that goes with it: *"that edge has a 700ms p99 and contributes zero milliseconds of user-visible latency, because it runs in parallel. Every service map I've used would have sent me to debug it."*

Four seconds of interaction carrying the entire product thesis. Build it early, keep it prominent, and make the transition **animated** — an instant re-render reads as a page change, whereas a 400ms eased transition reads as the same graph being re-judged, which is the point.

```tsx
type MetricMode = 'wall' | 'critical';
// Drive edge opacity from blameShare when mode === 'critical',
// and from a constant when mode === 'wall'. Transition, don't snap.
```

## Visual encoding

| Channel | Encodes | Why |
|---|---|---|
| Node size | request volume | Instant sense of traffic shape |
| Node colour | service error rate | Red reads as failing without a legend |
| Edge thickness | request rate | Thick equals hot path |
| Edge colour | error rate (green to amber to red) | Conventional, needs no explanation |
| **Edge opacity** | **critical-path blame share** | The differentiator, visually |
| Edge dash (animated) | currently above baseline | Motion draws the eye to live anomalies |
| Node badge | deployed within 15 minutes | Ties change intelligence into the map |

Resist adding a seventh channel. Six is already at the limit of what a viewer decodes in a screen-share, and the toggle is worth more than any additional encoding.

## Graph component rules

The force graph is a **client component** (`'use client'`) because D3 mutates the DOM directly. Keep everything else a server component.

Non-obvious things that make the difference between a demo and a toy:

- **Persist node positions across refreshes.** Key nodes by service name and carry `x`, `y`, `fx`, `fy` from the previous simulation into the next data load. Without this the layout re-seeds every 10 seconds and the graph visibly jumps — the single most common thing that makes a custom map look amateurish.
- **Freeze the simulation after convergence.** Call `simulation.alphaTarget(0)` and stop ticking once settled. A permanently jiggling graph is a CPU heater that will drain a laptop battery during an interview.
- **Clean up on unmount.** `simulation.stop()` and remove listeners in the effect cleanup, or navigating away leaves a running simulation behind.
- **Guard against empty data.** First render before any traffic must show a purposeful empty state, not a blank SVG. "Waiting for traffic — run `make demo`" is a better first impression than a white rectangle.
- **Layout threshold.** Force layout is fine below roughly 30 nodes. Beyond that switch to a dagre hierarchical layout with focus-plus-context. Know the number; "what happens at 200 services?" is a standard follow-up.

## Data contract

`GET /api/graph` returns a Redis-cached payload, so it is a sub-2ms hit. Poll every 10 seconds, matching the graph-builder's cadence — polling faster only burns cache reads without fresher data.

```ts
type GraphResponse = {
  computedAt: string;          // ISO — drives the staleness banner
  stale: boolean;              // true when the cache is serving last-good data
  nodes: Array<{
    service: string;
    requestRate: number;
    errorRate: number;
    deployedRecently: boolean;
  }>;
  edges: Array<{
    source: string;
    target: string;
    requestCount: number;
    errorRate: number;
    wallP50: number; wallP95: number; wallP99: number;
    critP99: number;           // critical-path p99
    blameShare: number;        // 0..1, drives opacity in critical mode
    anomalous: boolean;
  }>;
};
```

**Render the staleness banner.** When `stale` is true the map is showing last-good data from cache because the analytics tier is degraded. Showing stale numbers silently is worse than showing nothing — an honest banner is both better UX and a talking point about availability-over-consistency on the telemetry plane.

Validate the payload with Zod at the boundary. A schema change in the analytics API should fail loudly in one place, not produce `NaN` widths deep inside D3.

## Routes

| Route | Contents |
|---|---|
| `/` | The map, alert feed (SSE), metric-mode toggle, staleness banner |
| `/service/[name]` | Service RED metrics, its edges, recent deploys |
| `/edge/[src]/[dst]` | Latency histogram, wall vs critical time series, **deploy markers as vertical lines**, slow-trace list linking to Jaeger |
| `/traces/[id]` | Critical path as an ordered list with per-hop ms, "open in Jaeger" |
| `/anomalies` | History with deploy correlation and confidence |
| `/evals` | Detector scoreboard: precision, recall, MTTD over time, per-case outcomes |
| `/cost` | Telemetry cost model: bytes per span, projected spend, sampling savings |

`/evals` is worth building even if it is plain. A live scoreboard of the platform's own detection accuracy is the most unusual page in the project, and it is the one that reframes the work from "built a dashboard" to "engineers systematically."

## Alert feed

Use **SSE**, not WebSockets. The feed is one-directional server-to-client, SSE reconnects automatically, and it survives proxies that mishandle WebSocket upgrades. Cap the rendered list (say 50) and drop older entries client-side, or a busy incident grows the DOM without bound.

## Styling

shadcn/ui components are **copied into `components/ui`** and edited there — they are not an npm dependency. Preserve the Radix accessibility primitives when editing; keyboard navigation and focus rings are free until someone strips them.

Theme through CSS variables and semantic tokens, never hardcoded hex values. Ship dark mode and default to it — an SRE console that is blinding in a dim room reads as un-thought-through, and every screen-share of this project will happen in a dark IDE context.

## What not to do

- Do not use the Pages Router or start on Next.js 15.
- Do not render the force graph in a server component.
- Do not use `localStorage` or `sessionStorage` for graph state; keep it in React state.
- Do not re-seed the force layout on every poll.
- Do not poll faster than the graph-builder writes.
- Do not display stale data without the banner.
- Do not install `shadcn/ui` as a package dependency.

## Interview soundbites

- *"Edge opacity encodes critical-path blame share. Flip the toggle and the parallel edge fades out — that single interaction is the whole product thesis."*
- *"Node positions persist across refreshes. Without that the layout re-seeds every ten seconds and a genuinely correct graph looks broken."*
- *"When the analytics tier is degraded the map serves last-good data from Redis with a staleness banner. On the telemetry plane I'd rather be available and honestly stale than unavailable and consistent."*

## Pairing

- `tracemap-clickhouse-analytics` — the queries behind `/api/graph`
- `tracemap-critical-path` — where `blameShare` comes from
- `tracemap-deploy-attribution` — deploy markers on edge charts
