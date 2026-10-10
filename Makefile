.DEFAULT_GOAL := help
UV ?= uv

help:            ## show targets
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

install:         ## install dependencies
	$(UV) sync --extra dev

db-up:           ## start local PostGIS
	docker compose up -d --wait db

db-down:         ## stop local PostGIS (keeps data)
	docker compose stop db

migrate:         ## apply migrations
	$(UV) run hrm migrate

api:             ## run API on :8000 (auto-reload)
	$(UV) run hrm api --reload

frontend:        ## run Dash UI on :8050
	$(UV) run hrm frontend --debug

worker:          ## run analysis worker
	$(UV) run hrm worker

dispatcher:      ## run outbox dispatcher
	$(UV) run hrm dispatcher

up:              ## full stack in containers (UI http://localhost:8050)
	docker compose --profile app up --build

lint:            ## ruff + mypy
	$(UV) run ruff check src tests scripts deploy && $(UV) run ruff format --check src tests scripts deploy && $(UV) run mypy src

fmt:             ## format code
	$(UV) run ruff format src tests scripts deploy && $(UV) run ruff check --fix src tests scripts deploy

test:            ## unit tests (no database)
	$(UV) run pytest tests/unit

test-all:        ## unit + integration (needs `make db-up`)
	$(UV) run pytest tests --cov=resilience_map --cov-report=term-missing:skip-covered

image:           ## build the container image
	docker build -t resilience-map:dev .

.PHONY: help install db-up db-down migrate api frontend worker dispatcher up lint fmt test test-all image

clean:           ## remove generated caches and coverage artifacts only
	python3 scripts/clean_generated.py

.PHONY: clean
