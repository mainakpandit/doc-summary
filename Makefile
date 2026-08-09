SHELL := /bin/bash

ENV_FILE := .env
ENV_EXAMPLE := .env.example
LOG_DIR := .logs
BACKEND_LOG := $(LOG_DIR)/backend.log
FRONTEND_LOG := $(LOG_DIR)/frontend.log
DB_HEALTH_TIMEOUT := 30
SEED_SCRIPT := backend/scripts/seed_demo.py

.PHONY: dev test reset seed db-up db-down

# Fresh-clone-to-running-system in one command. See CLAUDE.md behavior 6.
dev: db-up
	uv sync
	@echo "Running database migrations..."
	@uv run alembic upgrade head || echo "WARNING: alembic upgrade head failed (expected until the Alembic init step lands) - continuing"
	@echo "Seeding demo corpus..."
	@if [ -f $(SEED_SCRIPT) ]; then \
		uv run python $(SEED_SCRIPT) || echo "WARNING: $(SEED_SCRIPT) failed - continuing"; \
	else \
		echo "$(SEED_SCRIPT) not found yet - skipping seed"; \
	fi
	@if [ ! -d frontend/node_modules ]; then \
		echo "Installing frontend dependencies..."; \
		(cd frontend && npm install); \
	fi
	@mkdir -p $(LOG_DIR)
	@echo "Starting backend -> $(BACKEND_LOG)"
	@nohup uv run uvicorn backend.app.main:app --reload --port 8000 > $(BACKEND_LOG) 2>&1 &
	@echo "Starting frontend -> $(FRONTEND_LOG)"
	@(cd frontend && nohup npm run dev > ../$(FRONTEND_LOG) 2>&1 &)
	@sleep 1
	@echo ""
	@echo "pm-analyst is up:"
	@echo "  Backend:  http://localhost:8000"
	@echo "  Frontend: http://localhost:5173"
	@echo "  Logs:     $(BACKEND_LOG)  $(FRONTEND_LOG)"

test:
	uv run pytest -q

reset:
	docker compose down -v
	rm -rf $(LOG_DIR)

seed:
	uv run python $(SEED_SCRIPT)

db-up:
	@if [ ! -f $(ENV_FILE) ]; then \
		cp $(ENV_EXAMPLE) $(ENV_FILE); \
		echo "Created $(ENV_FILE) from $(ENV_EXAMPLE)"; \
	fi
	docker compose up -d db
	@echo "Waiting for db healthcheck (max $(DB_HEALTH_TIMEOUT)s)..."
	@i=0; \
	until docker compose ps db 2>/dev/null | grep -q "healthy"; do \
		if [ $$i -ge $(DB_HEALTH_TIMEOUT) ]; then \
			echo "WARNING: db did not report healthy within $(DB_HEALTH_TIMEOUT)s - continuing anyway" >&2; \
			break; \
		fi; \
		sleep 1; \
		i=$$((i + 1)); \
	done
	@echo "db is up"

db-down:
	docker compose down
