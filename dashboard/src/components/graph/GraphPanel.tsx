"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import DependencyGraph from "@/components/graph/DependencyGraph";
import MetricModeToggle from "@/components/graph/MetricModeToggle";
import { API_BASE, GraphSchema, type Graph, type MetricMode } from "@/lib/types";

/** Matches the graph-builder's cadence. Polling faster only burns cache reads
 *  without producing fresher data - the blob does not change in between. */
const POLL_MS = 10_000;

export default function GraphPanel({ initial }: { initial: Graph }) {
  const [graph, setGraph] = useState<Graph>(initial);
  const [mode, setMode] = useState<MetricMode>("wall");
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  /* The metric mode is addressable as `?metric=critical`.
   *
   * Not a convenience: the point of this dashboard is to hand someone the edge
   * they should go fix, and a view you cannot link to is a view you have to
   * describe over a call. `?metric=critical` is the shareable form of "it is
   * not the service you think it is".
   *
   * Applied in an effect rather than as the useState initialiser, because the
   * server has no URL to read and would render "wall" while the client rendered
   * "critical" -- a hydration mismatch for the sake of one frame. The toggle
   * owns the value from here on; this only decides where it starts. */
  useEffect(() => {
    if (new URLSearchParams(window.location.search).get("metric") === "critical") {
      setMode("critical");
    }
  }, []);

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/graph`, { cache: "no-store" });
      if (!res.ok) throw new Error(`API returned ${res.status}`);
      setGraph(GraphSchema.parse(await res.json()));
      setError(null);
    } catch (e) {
      // Keep rendering the last good graph rather than blanking the canvas.
      setError(e instanceof Error ? e.message : "unreachable");
    }
  }, []);

  useEffect(() => {
    timer.current = setInterval(load, POLL_MS);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [load]);

  const ranked = [...graph.edges].sort((a, b) =>
    mode === "critical" ? b.critP99 - a.critP99 : b.wallP99 - a.wallP99,
  );

  return (
    <>
      <div className="panel-head">
        <div>
          <h2>Service dependencies</h2>
          <p className="sub">
            {graph.nodes.length} services · {graph.edges.length} edges · last{" "}
            {graph.windowMinutes ?? 5} min
          </p>
        </div>
        <MetricModeToggle mode={mode} onChange={setMode} />
      </div>

      {graph.stale && (
        <div className="banner banner-warn" role="status">
          <strong>Showing last-good data</strong> — the graph is{" "}
          {graph.ageSeconds?.toFixed(0)}s old. The analytics tier is degraded;
          these numbers are real but not current.
        </div>
      )}
      {error && (
        <div className="banner banner-err" role="status">
          <strong>API unreachable</strong> — {error}. Still rendering the last
          graph received.
        </div>
      )}

      <DependencyGraph graph={graph} mode={mode} />

      <div className="legend">
        <span><i className="sw sw-ok" /> &lt;1% errors</span>
        <span><i className="sw sw-warn" /> 1–5%</span>
        <span><i className="sw sw-bad" /> &gt;5%</span>
        <span className="legend-note">
          {mode === "critical"
            ? "Opacity = share of user-visible latency. Faded edges are slow but parallel."
            : "Wall clock — what every other service map shows."}
        </span>
      </div>

      <table className="edges">
        <thead>
          <tr>
            <th>Edge</th>
            <th className="num">Requests</th>
            <th className="num">Errors</th>
            <th className="num">Wall p99</th>
            <th className="num">Critical p99</th>
            <th className="num">Blame</th>
          </tr>
        </thead>
        <tbody>
          {ranked.map((e) => {
            const irrelevant = e.critP99 === 0 && e.wallP99 > 5;
            return (
              <tr key={`${e.source}->${e.target}`} className={irrelevant ? "is-faded" : ""}>
                <td className="mono">
                  {e.source} → {e.target}
                </td>
                <td className="num mono">{e.requestCount}</td>
                <td className="num mono">{(e.errorRate * 100).toFixed(1)}%</td>
                <td className="num mono">{e.wallP99.toFixed(1)}ms</td>
                <td className="num mono strong">{e.critP99.toFixed(1)}ms</td>
                <td className="num mono">{(e.blameShare * 100).toFixed(0)}%</td>
              </tr>
            );
          })}
          {ranked.length === 0 && (
            <tr>
              <td colSpan={6} className="empty-row">
                No edges yet — the assembler writes these ~45s after traffic.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </>
  );
}
