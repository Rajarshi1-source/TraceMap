# Go 1.26 build, distroless final stage (~12MB).
# No shell, no package manager, minimal CVE surface. The Node services cannot
# do this because the OTel agent needs the runtime.
FROM golang:1.26-alpine AS build
WORKDIR /src

# go.mod and go.sum are both committed, so the build is reproducible:
# `go mod download` verifies against recorded checksums instead of resolving
# fresh versions at build time the way `go mod tidy` would.
COPY go.mod go.sum ./
RUN go mod download

COPY . .
ARG GIT_SHA=dev-local
RUN CGO_ENABLED=0 go build -trimpath \
      -ldflags="-s -w -X main.gitSHA=${GIT_SHA}" \
      -o /out/product ./cmd/product

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/product /product
USER nonroot:nonroot
EXPOSE 8080
# Distroless has no shell, so the binary probes itself.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD ["/product", "-healthcheck"]
ENTRYPOINT ["/product"]
