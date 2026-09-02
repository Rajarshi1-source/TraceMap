package main

// Explicit OpenTelemetry wiring for product-svc.
//
// M11 - THIS FILE IS THE POINT. Node and Python get genuine zero-code
// instrumentation from a runtime agent that patches libraries at import time.
// Go is compiled with no runtime hook, so there is nothing to attach to: the
// tracer provider is constructed here and handlers are wrapped explicitly with
// otelgin middleware.
//
// Never write "zero code changes" as a blanket claim across the stack. The
// honest framing is that Go needs middleware or eBPF, and that limitation is
// precisely why the ecosystem is moving toward eBPF instrumentation (OBI).

import (
	"context"
	"errors"
	"os"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
)

// initTracing configures the global tracer provider and returns a shutdown
// function. It never returns an error that stops the service: a telemetry
// problem must not take down the application. That separation is the whole
// reason the collector runs out of process.
func initTracing(ctx context.Context, service, version string) func(context.Context) error {
	endpoint := os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
	if endpoint == "" {
		// No collector configured - run untraced rather than failing to boot.
		return func(context.Context) error { return nil }
	}

	exp, err := otlptracegrpc.New(ctx,
		otlptracegrpc.WithEndpointURL(endpoint),
		otlptracegrpc.WithTimeout(5*time.Second),
	)
	if err != nil {
		// Log and continue untraced. An observability failure is not an
		// application failure.
		return func(context.Context) error { return err }
	}

	res, err := resource.Merge(
		resource.Default(),
		resource.NewWithAttributes(
			semconv.SchemaURL,
			semconv.ServiceName(service),
			// M24 - service.version carries the git SHA. Deploy attribution
			// (Phase 6) and trace-to-code both key off this single attribute.
			semconv.ServiceVersion(version),
			attribute.String("deployment.environment", envStr("DEPLOY_ENV", "local")),
		),
	)
	if err != nil {
		res = resource.Default()
	}

	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exp,
			sdktrace.WithBatchTimeout(5*time.Second),
			sdktrace.WithMaxExportBatchSize(512),
		),
		sdktrace.WithResource(res),
		// Head sampling is OFF. The tail sampler in the collector gateway makes
		// the decision, once it can see the whole trace.
		sdktrace.WithSampler(sdktrace.ParentBased(sdktrace.AlwaysSample())),
	)

	otel.SetTracerProvider(tp)
	// W3C Trace Context: this is what stitches product-svc's spans to the
	// caller's trace via the traceparent header.
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{},
		propagation.Baggage{},
	))

	return func(ctx context.Context) error {
		shutdownCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
		defer cancel()
		return errors.Join(tp.Shutdown(shutdownCtx), exp.Shutdown(shutdownCtx))
	}
}
