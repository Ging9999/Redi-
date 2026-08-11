# Convenience commands. Everything is stdlib Python 3 — no runtime deps.
.PHONY: help test bench serve serve-local smoke doctor status join pyz \
        compose-up compose-logs docker-build clean

PYTHON ?= python3
PORT   ?= 8787
RUN    ?= $(PYTHON) -m redi

help:
	@echo "make test          run the full test suite"
	@echo "make bench         benchmark the PreToolUse hot path (docs/PERF.md)"
	@echo "make serve         run the server (token auto-generated to ./redi-data)"
	@echo "make serve-local   zero-config localhost server, open mode (trial)"
	@echo "make smoke         curl smoke test against a running server"
	@echo "make doctor        diagnose the local Redi setup"
	@echo "make status        show live claims for the current repo"
	@echo "make pyz           build the single-file dist/redi.pyz"
	@echo "make compose-up    docker compose up -d"
	@echo "make compose-logs  show the join string from the running server"
	@echo "make clean         remove data, build artifacts, and __pycache__"

test:
	$(PYTHON) -m unittest discover -s tests -p "test_*.py" -v

bench:
	$(PYTHON) tests/bench.py -n $(or $(N),100) --label "$(or $(LABEL),Run)"

serve:
	COORD_DATA_DIR=./redi-data COORD_DB=./redi-data/redi.db COORD_PORT=$(PORT) $(RUN) serve

serve-local:
	$(RUN) serve --local

smoke:
	COORD_URL=http://127.0.0.1:$(PORT) ./tests/smoke.sh

doctor:
	$(RUN) doctor

status:
	$(RUN) status

pyz:
	$(PYTHON) tools/build_pyz.py

compose-up:
	docker compose up -d

compose-logs:
	docker compose logs redi

docker-build:
	docker build -t redi .

clean:
	rm -rf redi-data dist build *.egg-info coordinator.db coordinator.db-* redi.db redi.db-*
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
