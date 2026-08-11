# Convenience commands. Everything is stdlib Python 3 — no install step.
.PHONY: help test bench serve serve-open smoke doctor status install install-user docker-build docker-run clean

PYTHON ?= python3
PORT   ?= 8787

help:
	@echo "make test          run the full test suite"
	@echo "make serve         run the server (token from COORD_TOKEN env)"
	@echo "make serve-open    run the server in open mode (no auth, dev only)"
	@echo "make smoke         curl smoke test against a running server"
	@echo "make doctor        diagnose the local Redi setup"
	@echo "make status        show live claims for the current repo"
	@echo "make install       install the hook into ./.claude/settings.json"
	@echo "make install-user  install the hook into ~/.claude/settings.json"
	@echo "make docker-build  build the server image"
	@echo "make docker-run    run the server image on port $(PORT)"
	@echo "make clean         remove the SQLite store and __pycache__"

test:
	$(PYTHON) -m unittest discover -s tests -p "test_*.py" -v

bench:
	$(PYTHON) tests/bench.py -n $(or $(N),100) --label "$(or $(LABEL),Run)"

serve:
	COORD_PORT=$(PORT) $(PYTHON) server/coordinator.py

serve-open:
	COORD_PORT=$(PORT) COORD_DB=:memory: $(PYTHON) server/coordinator.py

smoke:
	COORD_URL=http://127.0.0.1:$(PORT) ./tests/smoke.sh

doctor:
	$(PYTHON) cli/redi.py doctor

status:
	$(PYTHON) cli/redi.py status

install:
	$(PYTHON) hook/install.py

install-user:
	$(PYTHON) hook/install.py --user

docker-build:
	docker build -t agent-coordinator .

docker-run:
	docker run --rm -p $(PORT):8787 -e COORD_TOKEN="$${COORD_TOKEN:-}" -v coord-data:/data agent-coordinator

clean:
	rm -f coordinator.db coordinator.db-*
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
