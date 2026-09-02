.DEFAULT_GOAL := help
SHELL := /bin/bash
COMPOSE := docker compose

# GIT_SHA is resolved here and injected into every service as service.version.
# M24: this exists before any service does.
GIT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null || echo dev-local)
export GIT_SHA

.PHONY: help env up down demo seed logs ps smoke test skills skills-check fanout-guard probe-guard clean

help: ## Show this help
	@echo ""
	@echo "  TraceMap - $(GIT_SHA)"
	@echo ""
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo ""

env: ## Create .env from .env.example, or refresh GIT_SHA in an existing one
	@if [ ! -f .env ]; then 	  sed 's/^GIT_SHA=.*/GIT_SHA=$(GIT_SHA)/' .env.example > .env; 	  echo "  wrote .env  (GIT_SHA=$(GIT_SHA))"; 	else 	  sed -i.bak 's/^GIT_SHA=.*/GIT_SHA=$(GIT_SHA)/' .env && rm -f .env.bak; 	  echo "  refreshed .env GIT_SHA=$(GIT_SHA)  (other settings preserved)"; 	fi
	@# GIT_SHA must track HEAD. It becomes service.version on every span, and a
	@# stale value makes Phase 6 deploy attribution name the wrong commit --
	@# which is worse than naming none, because it looks authoritative.

up: env ## Boot the core profile and wait for health
	$(COMPOSE) --profile core up -d --wait

down: ## Stop everything and remove volumes
	$(COMPOSE) --profile core down -v

ps: ## Show container status
	$(COMPOSE) --profile core ps

logs: ## Tail logs from the core profile
	$(COMPOSE) --profile core logs -f --tail=80

demo: up seed ## Boot core and pre-load traffic - use this before a screen-share
	@echo ""
	@echo "  Gateway    http://localhost:3000"
	@echo ""

seed: ## 60s of load so the graph is populated on first look
	docker run --rm --network host -v "$(PWD)/load-testing:/scripts" \
	  grafana/k6 run --duration 60s --vus 5 /scripts/normal-checkout-flow.js

smoke: ## End-to-end smoke test against a running stack
	./scripts/smoke-test.sh

test: ## Run every service's unit tests
	cd microservices/order && npm test
	cd microservices/product && go test ./...

fanout-guard: ## M1 - prove the checkout fan-out is concurrent, not sequential
	cd microservices/order && npm test -- fanout

probe-guard: ## M17 - prove /healthz survives a dead database and /readyz does not
	./scripts/probe-guard.sh

trace-guard: ## Phase 2 - prove one trace spans all five services and the fan-out overlaps
	@python scripts/trace-guard.py --expect-sha $(GIT_SHA)

skills: ## Rebuild .claude/skills/*.skill from skills/ (D4 - archives are artifacts)
	@python scripts/build-skills.py

skills-check: ## Fail if any skill archive is stale (CI gate)
	@python scripts/build-skills.py --check

clean: ## Remove build artifacts and local data
	rm -rf microservices/*/node_modules microservices/product/product
	find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
