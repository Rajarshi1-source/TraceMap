.DEFAULT_GOAL := help
SHELL := /bin/bash
COMPOSE := docker compose

# GIT_SHA is resolved here and injected into every service as service.version.
# M24: this exists before any service does.
GIT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null || echo dev-local)
export GIT_SHA

.PHONY: help env up down demo seed logs ps smoke test skills skills-check fanout-guard probe-guard clean \n        eval eval-smoke eval-case eval-up eval-down eval-gate-guard eval-baseline \n        eval-check eval-tests topology-guard manifest-guard \n        helm-guard monitoring

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
	@echo "  Dashboard  http://localhost:3002"
	@echo "  Jaeger UI  http://localhost:16686"
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
	cd platform && python -m pytest -q

fanout-guard: ## M1 - prove the checkout fan-out is concurrent, not sequential
	cd microservices/order && npm test -- fanout

probe-guard: ## M17 - prove /healthz survives a dead database and /readyz does not
	./scripts/probe-guard.sh

trace-guard: ## Phase 2 - prove one trace spans all five services and the fan-out overlaps
	@python scripts/trace-guard.py --expect-sha $(GIT_SHA)

schema-guard: ## Phase 3 - assert the ClickHouse DDL invariants (M2/M3, F1, M4, M16)
	@python scripts/schema-guard.py

profile-matrix: ## Phase 3 - boot every Jaeger storage profile and prove traces land
	./scripts/profile-matrix.sh

critical-path-guard: ## Phase 4 - the flagship: parallel siblings score 0, payment does not
	@python scripts/critical-path-guard.py

api-guard: ## Phase 5 - assert the graph API contract and latency budget
	@python scripts/api-guard.py

guards: fanout-guard probe-guard trace-guard schema-guard critical-path-guard api-guard eval-gate-guard manifest-guard ## Run every invariant guard

# ---------------------------------------------------------------- Phase 7
#
# The detector is a model, so it gets a held-out set and a merge gate. Read
# docs/adr/0006-eval-gates-ci.md before changing a threshold: tuning without
# re-running the set is M8, and it is the one thing that would collapse the
# whole MLOps claim.

eval-up: env ## Boot the stack with background load, ready for an eval run
	$(COMPOSE) -f docker-compose.yml -f docker-compose.eval.yml --profile eval up -d --wait
	@echo ""
	@echo "  Warming baselines. Five detector cycles plus pipeline lag - give it"
	@echo "  ~6 minutes, or every case will score as a false negative because the"
	@echo "  warmup gate suppressed it (harness failure mode #1)."
	@echo ""

eval-down: ## Tear the eval stack down, volumes included
	$(COMPOSE) -f docker-compose.yml -f docker-compose.eval.yml --profile eval down -v

# The overlay publishes the datastores on non-default host ports so a native
# Postgres or Redis on the machine cannot shadow the containers. These exports
# point the runner at them.
EVAL_ENV := POSTGRES_HOST=localhost POSTGRES_PORT=$${EVAL_POSTGRES_PORT:-55432}             REDIS_URL=redis://localhost:$${EVAL_REDIS_PORT:-56379}/0

eval: ## Run the full golden set and print the scoreboard (~110 min)
	@$(EVAL_ENV) python -m eval.runner --tier full --git-sha $(GIT_SHA) 	  --output eval-report.md --json eval-run.json

eval-smoke: ## Run the 4-case smoke tier (~45 min) - what PRs gate on
	@$(EVAL_ENV) python -m eval.runner --tier smoke --git-sha $(GIT_SHA) 	  --output eval-report.md --json eval-run.json

eval-case: ## Run one case:  make eval-case CASE=006
	@$(EVAL_ENV) python -m eval.runner --case $(CASE) --git-sha $(GIT_SHA)

eval-check: ## Compare the last run against its tier's committed baseline
	@python -m eval.check_regression --current eval-run.json

eval-baseline: ## Record the last run AS its tier's baseline - only ever on main
	@python -m eval.check_regression --current eval-run.json --update
	@echo "  Commit this with a message saying WHY the baseline moved."

eval-gate-guard: ## Exit criterion 5 - prove the gate goes red on a broken detector
	@python scripts/eval-gate-guard.py

eval-tests: ## Fast offline checks: the case files, the scorer, the gate
	@python -m pytest eval/tests -q -c eval/pytest.ini --rootdir=.

manifest-guard: ## M10/M18 - no Jaeger v1 manifests, no EOL base images
	@python scripts/manifest-guard.py

topology-guard: ## Assert the declared service graph still matches the live one
	@python scripts/topology-guard.py

helm-guard: ## M17/M20/M23/M24 asserted against the rendered chart
	@helm template tracemap charts/tracemap > /tmp/tracemap-rendered.yaml
	@python scripts/helm-guard.py --manifest /tmp/tracemap-rendered.yaml

monitoring: env ## Boot core + Prometheus and Grafana (the `full` profile)
	$(COMPOSE) --profile full up -d --wait
	@echo ""
	@echo "  Grafana     http://localhost:3003  (admin / $$GRAFANA_PASSWORD)"
	@echo "  Prometheus  http://localhost:9090"
	@echo ""

skills: ## Rebuild .claude/skills/*.skill from skills/ (D4 - archives are artifacts)
	@python scripts/build-skills.py

skills-check: ## Fail if any skill archive is stale (CI gate)
	@python scripts/build-skills.py --check

clean: ## Remove build artifacts and local data
	rm -rf microservices/*/node_modules microservices/product/product
	find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
