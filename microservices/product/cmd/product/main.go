// product-svc - stock checks, with a deliberate N+1 above a cart-size
// threshold.
//
// The N+1 is not sloppiness. It gives the Jaeger waterfall in Phase 2 something
// genuinely diagnosable - twelve identical queries where there should have been
// one - which is both the drill-in demo and the raw material for
// POSTMORTEM-001.
//
// Go has NO runtime auto-instrumentation agent (M11). Phase 2 wraps these
// handlers explicitly with otelgin middleware; never claim zero-code
// instrumentation for Go.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"math/rand"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5/pgxpool"
)

// gitSHA is injected at build time via -ldflags.
var gitSHA = "dev-local"

type server struct {
	pool      *pgxpool.Pool
	baseMS    int
	nPlusOne  int
	faults    *FaultConfig
	service   string
	version   string
}

type stockRequest struct {
	Items []struct {
		SKU string `json:"sku"`
		Qty int    `json:"qty"`
	} `json:"items"`
}

func main() {
	healthcheck := flag.Bool("healthcheck", false, "probe localhost /healthz and exit")
	flag.Parse()

	port := envStr("PORT", "8080")

	// Distroless has no shell and no wget, so the container healthcheck
	// re-executes this same binary with -healthcheck instead.
	if *healthcheck {
		os.Exit(probe(port))
	}

	ctx := context.Background()
	pool, err := pgxpool.New(ctx, dsn())
	if err != nil {
		log.Fatalf("pgxpool: %v", err)
	}
	defer pool.Close()

	s := &server{
		pool:     pool,
		baseMS:   envInt("SVC_PRODUCT_BASE_MS", 45),
		nPlusOne: envInt("PRODUCT_N_PLUS_ONE_THRESHOLD", 3),
		faults:   NewFaultConfig(),
		service:  envStr("SERVICE_NAME", "product-svc"),
		version:  envStr("SERVICE_VERSION", gitSHA),
	}

	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	r.Use(gin.Recovery())
	// Phase 2: r.Use(otelgin.Middleware("product-svc")) - explicit, because no
	// runtime agent exists for Go.

	r.POST("/stock/check", s.checkStock)
	r.GET("/products", s.listProducts)
	r.GET("/healthz", s.healthz)
	r.GET("/readyz", s.readyz)
	r.POST("/_internal/fault", s.setFault)
	r.DELETE("/_internal/fault", s.clearFault)
	r.GET("/_internal/fault", s.getFault)

	log.Printf(`{"level":"info","service":%q,"version":%q,"msg":"listening on :%s"}`,
		s.service, s.version, port)
	if err := r.Run(":" + port); err != nil {
		log.Fatalf("run: %v", err)
	}
}

// ---------------------------------------------------------------- handlers

func (s *server) checkStock(c *gin.Context) {
	var req stockRequest
	if err := c.ShouldBindJSON(&req); err != nil || len(req.Items) == 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "bad_request"})
		return
	}

	f := s.faults.ForEndpoint("/stock/check")
	if f.CPUBurnMS > 0 {
		burnCPU(f.CPUBurnMS)
	}
	time.Sleep(time.Duration(s.baseMS+f.DelayMS) * time.Millisecond)
	if f.ErrorRate > 0 && rand.Float64() < f.ErrorRate {
		c.JSON(http.StatusBadGateway, gin.H{"error": "injected_fault"})
		return
	}

	ctx := c.Request.Context()
	unavailable := []string{}

	if len(req.Items) > s.nPlusOne {
		// The deliberate N+1: one query per item, so the waterfall shows a row
		// of identical sibling DB spans.
		for _, item := range req.Items {
			ok, err := s.checkOne(ctx, item.SKU, item.Qty)
			if err != nil {
				c.JSON(http.StatusInternalServerError, gin.H{"error": "db"})
				return
			}
			if !ok {
				unavailable = append(unavailable, item.SKU)
			}
		}
		c.JSON(http.StatusOK, gin.H{"available": len(unavailable) == 0, "unavailable": unavailable, "path": "n_plus_one"})
		return
	}

	// The good path: one batched query.
	skus := make([]string, 0, len(req.Items))
	want := make(map[string]int, len(req.Items))
	for _, item := range req.Items {
		skus = append(skus, item.SKU)
		want[item.SKU] += item.Qty
	}

	rows, err := s.pool.Query(ctx,
		`SELECT sku, stock FROM products WHERE sku = ANY($1)`, skus)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "db"})
		return
	}
	defer rows.Close()

	seen := map[string]int{}
	for rows.Next() {
		var sku string
		var stock int
		if err := rows.Scan(&sku, &stock); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "db"})
			return
		}
		seen[sku] = stock
	}
	for sku, qty := range want {
		if seen[sku] < qty {
			unavailable = append(unavailable, sku)
		}
	}

	c.JSON(http.StatusOK, gin.H{"available": len(unavailable) == 0, "unavailable": unavailable, "path": "batched"})
}

func (s *server) checkOne(ctx context.Context, sku string, qty int) (bool, error) {
	var stock int
	err := s.pool.QueryRow(ctx,
		`SELECT stock FROM products WHERE sku = $1`, sku).Scan(&stock)
	if err != nil {
		return false, err
	}
	return stock >= qty, nil
}

func (s *server) listProducts(c *gin.Context) {
	rows, err := s.pool.Query(c.Request.Context(),
		`SELECT sku, name, price_cents, stock FROM products ORDER BY sku LIMIT 100`)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "db"})
		return
	}
	defer rows.Close()

	out := []gin.H{}
	for rows.Next() {
		var sku, name string
		var price, stock int
		if err := rows.Scan(&sku, &name, &price, &stock); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "db"})
			return
		}
		out = append(out, gin.H{"sku": sku, "name": name, "priceCents": price, "stock": stock})
	}
	c.JSON(http.StatusOK, gin.H{"products": out})
}

// ------------------------------------------------------------------ probes

// healthz answers "should I be restarted?" - process liveness ONLY. Checking
// the database here would make one slow query restart the whole fleet (M17).
func (s *server) healthz(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{"status": "ok", "service": s.service, "version": s.version})
}

// readyz answers "should I get traffic?" - dependencies are checked here.
func (s *server) readyz(c *gin.Context) {
	ctx, cancel := context.WithTimeout(c.Request.Context(), 2*time.Second)
	defer cancel()
	if err := s.pool.Ping(ctx); err != nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{"status": "not_ready", "reason": "db_unreachable"})
		return
	}
	c.JSON(http.StatusOK, gin.H{"status": "ready"})
}

// ----------------------------------------------------------- fault control

func (s *server) setFault(c *gin.Context) {
	var body struct {
		DelayMS   int     `json:"delay_ms"`
		ErrorRate float64 `json:"error_rate"`
		CPUBurnMS int     `json:"cpu_burn_ms"`
		Endpoint  string  `json:"endpoint"`
		TTLS      int     `json:"ttl_s"`
	}
	if err := c.ShouldBindJSON(&body); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "bad_request"})
		return
	}
	if body.TTLS == 0 {
		body.TTLS = 600
	}
	s.faults.Set(Fault{DelayMS: body.DelayMS, ErrorRate: body.ErrorRate, CPUBurnMS: body.CPUBurnMS},
		body.Endpoint, body.TTLS)
	c.JSON(http.StatusOK, s.faults.Describe())
}

func (s *server) clearFault(c *gin.Context) {
	s.faults.Clear()
	c.JSON(http.StatusOK, s.faults.Describe())
}

func (s *server) getFault(c *gin.Context) {
	c.JSON(http.StatusOK, s.faults.Describe())
}

// ------------------------------------------------------------------ helpers

func probe(port string) int {
	client := http.Client{Timeout: 2 * time.Second}
	resp, err := client.Get("http://localhost:" + port + "/healthz")
	if err != nil {
		return 1
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return 1
	}
	return 0
}

func dsn() string {
	return fmt.Sprintf("postgres://%s:%s@%s:%s/%s",
		envStr("POSTGRES_USER", "tracemap"),
		envStr("POSTGRES_PASSWORD", "change-me-locally"),
		envStr("POSTGRES_HOST", "postgres"),
		envStr("POSTGRES_PORT", "5432"),
		envStr("POSTGRES_DB", "tracemap"),
	)
}

func envStr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func envInt(k string, def int) int {
	if v := os.Getenv(k); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func burnCPU(ms int) {
	deadline := time.Now().Add(time.Duration(ms) * time.Millisecond)
	for time.Now().Before(deadline) {
	}
}
