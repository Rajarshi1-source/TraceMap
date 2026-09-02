"use client";

/**
 * The dependency map. A client component, because D3 mutates the DOM directly.
 *
 * Three rules here are the difference between a demo and a toy, and all three
 * are easy to get wrong in a way that still "works":
 *
 *  1. NODE POSITIONS PERSIST ACROSS REFRESHES (M25). The data reloads every
 *     10s. If the simulation is re-seeded each time, the layout jumps and a
 *     completely correct graph looks broken. Positions are carried forward by
 *     service name.
 *
 *  2. THE SIMULATION FREEZES once settled. A permanently ticking force layout
 *     is a CPU heater that drains a laptop during a screen-share.
 *
 *  3. IT CLEANS UP on unmount. A simulation left running after navigation keeps
 *     ticking against a detached DOM.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import * as d3 from "d3";

import type { Edge, Graph, MetricMode, ServiceNode } from "@/lib/types";

type SimNode = ServiceNode & d3.SimulationNodeDatum;
/**
 * d3-force REWRITES `source` and `target` in place: they arrive as service-name
 * strings and are replaced with node objects once the link force resolves them.
 * Omitting the string fields from Edge and taking d3's own union is what makes
 * that honest to the type checker instead of a cast.
 */
type SimLink = Omit<Edge, "source" | "target"> & d3.SimulationLinkDatum<SimNode>;

const WIDTH = 900;
const HEIGHT = 520;

/** Force layout is readable up to ~30 nodes; past that it becomes a hairball. */
const FORCE_LAYOUT_LIMIT = 30;

function errorColor(rate: number): string {
  if (rate >= 0.05) return "#c2453c";
  if (rate > 0.01) return "#b8792f";
  return "#3f8f6b";
}

export default function DependencyGraph({
  graph,
  mode,
}: {
  graph: Graph;
  mode: MetricMode;
}) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const simRef = useRef<d3.Simulation<SimNode, undefined> | null>(null);
  /** Survives data reloads. This is what stops the layout jumping. */
  const positions = useRef<Map<string, { x: number; y: number }>>(new Map());
  const [selected, setSelected] = useState<string | null>(null);

  const maxRequests = useMemo(
    () => Math.max(1, ...graph.nodes.map((n) => n.requestCount)),
    [graph.nodes],
  );
  const maxEdgeRate = useMemo(
    () => Math.max(1, ...graph.edges.map((e) => e.requestCount)),
    [graph.edges],
  );

  useEffect(() => {
    if (!svgRef.current || graph.nodes.length === 0) return;

    const nodes: SimNode[] = graph.nodes.map((n) => {
      const prev = positions.current.get(n.service);
      return { ...n, x: prev?.x, y: prev?.y };
    });
    const byName = new Map(nodes.map((n) => [n.service, n]));
    const links: SimLink[] = graph.edges
      .filter((e) => byName.has(e.source) && byName.has(e.target))
      .map<SimLink>((e) => ({
        ...e,
        source: byName.get(e.source) as SimNode,
        target: byName.get(e.target) as SimNode,
      }));

    const svg = d3.select(svgRef.current);
    svg.selectAll("*").remove();

    // One marker per error band. Markers cannot inherit the line's stroke -
    // `currentColor` resolves against the marker element, not the referencing
    // line - so a shared marker renders grey however the edge is coloured.
    // markerUnits=userSpaceOnUse stops the head scaling with stroke-width,
    // which otherwise makes a thick edge sprout an enormous arrow.
    const defs = svg.append("defs");
    const bands: Array<[string, string]> = [
      ["ok", "#3f8f6b"],
      ["warn", "#b8792f"],
      ["bad", "#c2453c"],
    ];
    for (const [name, color] of bands) {
      defs
        .append("marker")
        .attr("id", `arrow-${name}`)
        .attr("viewBox", "0 -5 10 10")
        .attr("refX", 34)
        .attr("markerUnits", "userSpaceOnUse")
        .attr("markerWidth", 11)
        .attr("markerHeight", 11)
        .attr("orient", "auto")
        .append("path")
        .attr("d", "M0,-4L9,0L0,4")
        .attr("fill", color);
    }

    const band = (rate: number) => (rate >= 0.05 ? "bad" : rate > 0.01 ? "warn" : "ok");

    const g = svg.append("g");

    const link = g
      .append("g")
      .attr("class", "links")
      .selectAll<SVGLineElement, SimLink>("line")
      .data(links)
      .join("line")
      .attr("stroke", (d) => errorColor(d.errorRate))
      .attr("stroke-width", (d) => 1 + (d.requestCount / maxEdgeRate) * 5)
      .attr("marker-end", (d) => `url(#arrow-${band(d.errorRate)})`)
      .style("transition", "opacity 400ms ease, stroke-width 400ms ease");

    const labels = g
      .append("g")
      .attr("class", "edge-labels")
      .selectAll<SVGTextElement, SimLink>("text")
      .data(links)
      .join("text")
      .attr("font-size", 10)
      .attr("font-family", "ui-monospace, monospace")
      .attr("fill", "var(--ink-soft)")
      .attr("text-anchor", "middle")
      .style("pointer-events", "none")
      .style("transition", "opacity 400ms ease");

    const node = g
      .append("g")
      .attr("class", "nodes")
      .selectAll<SVGGElement, SimNode>("g")
      .data(nodes)
      .join("g")
      .style("cursor", "pointer")
      .on("click", (_e, d) => setSelected((s) => (s === d.service ? null : d.service)));

    node
      .append("circle")
      .attr("r", (d) => 14 + (d.requestCount / maxRequests) * 16)
      .attr("fill", (d) => errorColor(d.errorRate))
      .attr("fill-opacity", 0.22)
      .attr("stroke", (d) => errorColor(d.errorRate))
      .attr("stroke-width", 2);

    node
      .append("text")
      .text((d) => d.service.replace(/-svc$/, ""))
      .attr("text-anchor", "middle")
      .attr("dy", 4)
      .attr("font-size", 11)
      .attr("font-weight", 600)
      .attr("fill", "var(--ink)")
      .style("pointer-events", "none");

    const sim = d3
      .forceSimulation<SimNode>(nodes)
      .force("link", d3.forceLink<SimNode, SimLink>(links).id((d) => d.service).distance(190).strength(0.5))
      .force("charge", d3.forceManyBody().strength(-900))
      .force("center", d3.forceCenter(WIDTH / 2, HEIGHT / 2))
      .force("collide", d3.forceCollide<SimNode>().radius(52));

    // Seed from the previous layout so a reload CONTINUES rather than restarts.
    //
    // The alpha on a warm start is deliberately tiny. At 0.25 the graph still
    // visibly settles on every 10s poll - not the wild re-seed M25 warns about,
    // but enough motion to read as instability. Nodes that are already in a
    // good place should stay put; the residual energy exists only to make room
    // when a NEW service appears.
    const warm = positions.current.size > 0;
    sim.alpha(warm ? 0.03 : 1).alphaDecay(warm ? 0.1 : 0.0228);

    sim.on("tick", () => {
      link
        .attr("x1", (d) => (d.source as SimNode).x ?? 0)
        .attr("y1", (d) => (d.source as SimNode).y ?? 0)
        .attr("x2", (d) => (d.target as SimNode).x ?? 0)
        .attr("y2", (d) => (d.target as SimNode).y ?? 0);
      labels
        .attr("x", (d) => (((d.source as SimNode).x ?? 0) + ((d.target as SimNode).x ?? 0)) / 2)
        .attr("y", (d) => (((d.source as SimNode).y ?? 0) + ((d.target as SimNode).y ?? 0)) / 2 - 10);
      node.attr("transform", (d) => `translate(${d.x ?? 0},${d.y ?? 0})`);

      // Remember positions on every tick, not only on "end". d3-timer runs on
      // requestAnimationFrame, which browsers throttle to nothing in a hidden
      // tab - so a graph that loads in a background tab may never reach "end".
      // Saving only there means the layout re-seeds from scratch when the user
      // comes back, which is exactly the jump M25 exists to prevent.
      for (const n of nodes) {
        if (n.x != null && n.y != null) positions.current.set(n.service, { x: n.x, y: n.y });
      }
    });

    sim.on("end", () => {
      // Converged - stop burning CPU. A permanently ticking force layout is a
      // laptop-battery drain during a screen-share.
      sim.alphaTarget(0);
    });

    node.call(
      d3
        .drag<SVGGElement, SimNode>()
        .on("start", (event, d) => {
          if (!event.active) sim.alphaTarget(0.2).restart();
          d.fx = d.x;
          d.fy = d.y;
        })
        .on("drag", (event, d) => {
          d.fx = event.x;
          d.fy = event.y;
        })
        .on("end", (event, d) => {
          if (!event.active) sim.alphaTarget(0);
          d.fx = null;
          d.fy = null;
          if (d.x != null && d.y != null) positions.current.set(d.service, { x: d.x, y: d.y });
        }),
    );

    simRef.current = sim;
    return () => {
      // Without this the simulation keeps ticking against a detached DOM.
      sim.stop();
      simRef.current = null;
    };
  }, [graph, maxRequests, maxEdgeRate]);

  // Mode changes only restyle: no re-layout, so the graph is visibly the SAME
  // graph being re-judged rather than a new page.
  useEffect(() => {
    if (!svgRef.current) return;
    const svg = d3.select(svgRef.current);
    const maxBlame = Math.max(0.0001, ...graph.edges.map((e) => e.blameShare));

    svg
      .selectAll<SVGLineElement, SimLink>(".links line")
      .style("opacity", (d) =>
        mode === "critical" ? 0.1 + 0.9 * Math.sqrt(d.blameShare / maxBlame) : 0.85,
      );

    svg
      .selectAll<SVGTextElement, SimLink>(".edge-labels text")
      .text((d) =>
        mode === "critical" ? `${d.critP99.toFixed(0)}ms` : `${d.wallP99.toFixed(0)}ms`,
      )
      .style("opacity", (d) =>
        mode === "critical" ? (d.blameShare < 0.02 ? 0.35 : 1) : 1,
      );

    svg
      .selectAll<SVGGElement, SimNode>(".nodes g")
      .style("opacity", (d) =>
        selected && d.service !== selected ? 0.35 : 1,
      );
  }, [mode, graph, selected]);

  if (graph.nodes.length === 0) {
    // A purposeful empty state. A blank SVG reads as "broken", and the first
    // impression of a cold boot is hard to recover from.
    return (
      <div className="empty-state">
        <p className="empty-title">Waiting for traffic</p>
        <p className="empty-body">
          No traced requests in the last {graph.windowMinutes ?? 5} minutes.
          Run <code>make demo</code> to seed the graph.
        </p>
      </div>
    );
  }

  return (
    <div className="graph-wrap">
      <svg
        ref={svgRef}
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-label="Service dependency graph"
        className="graph-svg"
      />
      {graph.nodes.length > FORCE_LAYOUT_LIMIT && (
        <p className="graph-note">
          {graph.nodes.length} services — past ~{FORCE_LAYOUT_LIMIT} a force layout
          becomes unreadable; this is where a dagre hierarchical layout takes over.
        </p>
      )}
    </div>
  );
}
