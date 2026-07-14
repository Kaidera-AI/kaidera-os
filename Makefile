# Kaidera OS - operator make targets for the local Cortex + console stack.
#
# Scope: the configured Docker Compose project ONLY (cortex-* + the
# harness app-DB + the headless console). This is the local machine's dogfood
# stack - NOT the Kaidera AI platform deployment and NOT any customer deployment.
#
# Milestone 1 (T13) made the console a headless Docker service: live run-state
# lives in the durable app-DB (run_state/run_span), so the console can be rebuilt
# / restarted mid-run without losing what each agent is doing.

COMPOSE_FILE := .agents/docker-compose.cortex.yml
PROJECT      ?= $(if $(KAIDERA_COMPOSE_PROJECT),$(KAIDERA_COMPOSE_PROJECT),kaidera-os-cortex)
COMPOSE      := docker compose -p $(PROJECT) -f $(COMPOSE_FILE)

# The headless console's loopback health endpoint (the full shell on "/").
CONSOLE_URL  := http://127.0.0.1:8765/
HEALTH_TRIES := 30
HEALTH_SLEEP := 2

.DEFAULT_GOAL := help

.PHONY: help rebuild up down build logs ps health migrate config qa

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

rebuild: ## Full cycle: down -> build -> up -d (migrate one-shot runs) -> health-wait. One command.
	@echo "── Kaidera OS rebuild ($(PROJECT)) ──────────────────────────────"
	$(COMPOSE) down
	$(COMPOSE) build
	$(COMPOSE) up -d
	@echo "── waiting for the console to come healthy ($(CONSOLE_URL)) ──"
	@$(MAKE) --no-print-directory health
	@echo "✅ rebuild complete — stack is up and the console is serving"
	@echo "   (the harness-appdb-migrate one-shot converged the app-DB schema before the console booted)"
	$(COMPOSE) ps

up: ## Start the stack detached (runs the migrate one-shot, then the console)
	$(COMPOSE) up -d

down: ## Stop + remove the stack's containers (volumes are preserved)
	$(COMPOSE) down

build: ## Build the images (console + cortex-api + workers)
	$(COMPOSE) build

migrate: ## Run ONLY the app-DB schema one-shot (idempotent; applies .agents/data/appdb/*.sql)
	$(COMPOSE) up --no-deps harness-appdb-migrate

config: ## Validate the compose file (render the fully-interpolated config)
	$(COMPOSE) config

ps: ## Show stack container status
	$(COMPOSE) ps

logs: ## Tail the console logs (override SVC=... for another service)
	$(COMPOSE) logs -f $(or $(SVC),console)

health: ## Poll the console health endpoint until it answers (or time out)
	@i=0; \
	while [ $$i -lt $(HEALTH_TRIES) ]; do \
	  if curl -fs -o /dev/null --max-time 3 "$(CONSOLE_URL)"; then \
	    echo "  ✅ console healthy at $(CONSOLE_URL)"; exit 0; \
	  fi; \
	  i=$$((i + 1)); \
	  echo "  … console not ready yet (attempt $$i/$(HEALTH_TRIES)) — sleeping $(HEALTH_SLEEP)s"; \
	  sleep $(HEALTH_SLEEP); \
	done; \
	echo "  ❌ console did not come healthy within $$(($(HEALTH_TRIES) * $(HEALTH_SLEEP)))s"; \
	$(COMPOSE) ps; \
	exit 1

qa: ## Run the complete local static, backend, SPA, native, and release QA suite
	bash scripts/qa.sh
