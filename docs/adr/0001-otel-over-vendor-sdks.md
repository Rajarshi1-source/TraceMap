# ADR 0001 — OpenTelemetry over vendor SDKs

- **Status:** Accepted
- **Date:** 2026-09-02 (Phase 2)
- **Deciders:** Rajarshi Basu

## Context

Five services in three languages need to emit traces. The instrumentation
choice is effectively permanent: it touches every service, and changing it later
means re-instrumenting all of them at once.

The candidates were vendor SDKs (Datadog, New Relic, Honeycomb's own libraries),
Jaeger's historical client libraries, and OpenTelemetry.

## Decision

**OpenTelemetry SDKs everywhere, exporting OTLP to a collector.**

Jaeger's own client libraries were deprecated years ago in favour of OTel, so
"use Jaeger's SDK" is no longer a real option — the project that owns the
backend recommends against it.

The decisive property is that the **backend becomes an exporter setting rather
than an application dependency**. Swapping Jaeger for Tempo, or adding a
commercial backend alongside it, is a change to one collector config file. No
service is rebuilt and no application code is touched. That is what makes the
storage-profile work in ADR 0002 possible at all.

## Consequences

**Good.** Vendor-neutral and CNCF-governed. One instrumentation model across
Node, Python and Go. W3C Trace Context propagates across all three without
custom glue — verified in Phase 2 by a single 28-span trace crossing
Node → Python → Go and back.

**Bad.** OTel's Node and Python packages move fast and their versions are not
intuitive: the SDK and instrumentation packages sit on a `0.x` line while the
API is `1.x`. Pinning by guesswork fails — the first Phase 2 build broke on a
version that did not exist. Pins are now taken from the registry, not assumed.

**The caveat that must not be overstated.** Node and Python get *genuine*
zero-code instrumentation from a runtime agent that patches libraries at import
time. **Go does not.** It compiles to a static binary with no runtime hook, so
`product-svc` wraps its handlers explicitly with `otelgin` middleware and
attaches an `otelpgx` tracer to its connection pool — about fifteen lines in
`cmd/product/tracing.go`, written to be visible rather than hidden.

Claiming "zero code changes" across the whole stack would be false, and anyone
who writes Go would catch it immediately. The honest framing is better anyway:
the limitation is exactly why the ecosystem is moving toward eBPF
instrumentation, which is the motivation for the OBI stretch item.
