package main

// Runtime-mutable fault state with a TTL backstop (M23).
//
// Faults are read PER REQUEST. Never restart to apply one: a restart perturbs
// latency on its own, so restarting to inject a latency fault measures the
// restart.

import (
	"os"
	"strconv"
	"sync"
	"time"
)

type Fault struct {
	DelayMS   int     `json:"delay_ms"`
	ErrorRate float64 `json:"error_rate"`
	CPUBurnMS int     `json:"cpu_burn_ms"`
}

func (f Fault) active() bool {
	return f.DelayMS != 0 || f.ErrorRate != 0 || f.CPUBurnMS != 0
}

type FaultConfig struct {
	mu        sync.RWMutex
	fault     Fault
	endpoint  string
	expiresAt time.Time
}

func NewFaultConfig() *FaultConfig {
	fc := &FaultConfig{endpoint: os.Getenv("TRACEMAP_FAULT_ENDPOINT")}

	// Everything defaults to zero/off. A fault must never be on by accident.
	f := Fault{
		DelayMS:   envIntDefault("TRACEMAP_FAULT_DELAY_MS", 0),
		ErrorRate: envFloatDefault("TRACEMAP_FAULT_ERROR_RATE", 0),
		CPUBurnMS: envIntDefault("TRACEMAP_FAULT_CPU_BURN_MS", 0),
	}
	if f.active() {
		fc.Set(f, fc.endpoint, envIntDefault("TRACEMAP_FAULT_TTL_S", 600))
	}
	return fc
}

func (fc *FaultConfig) Set(f Fault, endpoint string, ttlS int) {
	fc.mu.Lock()
	defer fc.mu.Unlock()
	fc.fault = f
	fc.endpoint = endpoint
	fc.expiresAt = time.Now().Add(time.Duration(ttlS) * time.Second)
}

// Clear is idempotent - it is called from cleanup paths, possibly twice.
func (fc *FaultConfig) Clear() {
	fc.mu.Lock()
	defer fc.mu.Unlock()
	fc.fault = Fault{}
	fc.endpoint = ""
	fc.expiresAt = time.Time{}
}

func (fc *FaultConfig) ForEndpoint(path string) Fault {
	fc.mu.RLock()
	defer fc.mu.RUnlock()
	if time.Now().After(fc.expiresAt) {
		return Fault{} // expired: heals automatically
	}
	if fc.endpoint != "" && fc.endpoint != path {
		return Fault{} // scoped to one endpoint
	}
	return fc.fault
}

func (fc *FaultConfig) Describe() map[string]any {
	fc.mu.RLock()
	defer fc.mu.RUnlock()
	active := time.Now().Before(fc.expiresAt)
	f := fc.fault
	if !active {
		f = Fault{}
	}
	var endpoint any
	if fc.endpoint != "" {
		endpoint = fc.endpoint
	}
	expires := 0
	if active {
		expires = int(time.Until(fc.expiresAt).Seconds())
	}
	return map[string]any{
		"active":       active,
		"endpoint":     endpoint,
		"expires_in_s": expires,
		"delay_ms":     f.DelayMS,
		"error_rate":   f.ErrorRate,
		"cpu_burn_ms":  f.CPUBurnMS,
	}
}

func envIntDefault(k string, def int) int {
	if v := os.Getenv(k); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func envFloatDefault(k string, def float64) float64 {
	if v := os.Getenv(k); v != "" {
		if n, err := strconv.ParseFloat(v, 64); err == nil {
			return n
		}
	}
	return def
}
