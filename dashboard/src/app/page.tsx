import GraphPanel from "@/components/graph/GraphPanel";
import { GraphSchema, type Graph } from "@/lib/types";

// Always fetch fresh on the server: this page's whole job is showing current state.
export const dynamic = "force-dynamic";

const EMPTY: Graph = { nodes: [], edges: [], windowMinutes: 5 };

/**
 * Server component. It fetches the first paint server-side so the page never
 * flashes empty, then hands off to a client component for polling and D3.
 *
 * D3 mutates the DOM, so the graph itself MUST be a client component - but
 * everything around it stays on the server.
 */
async function fetchGraph(): Promise<Graph> {
  const base = process.env.ANALYTICS_API_URL ?? "http://localhost:8090";
  try {
    const res = await fetch(`${base}/api/graph`, { cache: "no-store" });
    if (!res.ok) return EMPTY;
    return GraphSchema.parse(await res.json());
  } catch {
    // A cold boot where the API is not up yet renders the empty state rather
    // than a stack trace.
    return EMPTY;
  }
}

export default async function Home() {
  const graph = await fetchGraph();

  return (
    <main className="wrap">
      <header className="mast">
        <div className="eyebrow">TraceMap</div>
        <h1>Which service should you actually go fix?</h1>
        <p className="dek">
          Every service map answers <em>which edge is slow</em>. In a parallel
          fan-out that is the wrong question — a call running alongside a slower
          sibling contributes <strong>zero</strong> user-visible latency. This
          map computes the critical path through every trace, so a slow-but-
          parallel edge fades instead of shouting.
        </p>
      </header>

      <section className="panel">
        <GraphPanel initial={graph} />
      </section>

      <footer className="foot">
        Wall clock is what Tempo, SigNoz, Coroot and Kiali all show. Critical
        path is the difference.
      </footer>
    </main>
  );
}
