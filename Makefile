PY := .venv/bin/python

.PHONY: venv install install-pip test gate workspace-acceptance clean

venv:
	python3 -m venv .venv

# Install the exact `uv.lock` environment validated on Python 3.12 and 3.14.
install:
	uv sync --frozen

# Export the lock as pip constraints without the editable project or hashes.
install-pip: venv
	uv export --frozen --no-emit-project --no-hashes --format requirements-txt > constraints.txt
	$(PY) -m pip install -q -e . -c constraints.txt

test:
	$(PY) -m unittest discover -s tests -v

# Run the real-data DuckDB gate and write `ELT_TASKGEN_GATE_REPORT`.
gate:
	$(PY) -m unittest tests.test_semantic_gate -v

# Offline, fail-closed L1 acceptance lane for the real dbt/scorer pilots.
workspace-acceptance:
	env UV_OFFLINE=1 uv sync --project runtime-images/dbt-duckdb --locked
	$(PY) -m unittest tests.test_training_terraform_intent tests.test_training_local_sync tests.test_training_protocol_proxy tests.test_training_dbt_runner tests.test_training_scorer -v

# Remove only the default scratch workspace and local build caches.
clean:
	rm -rf runs/default .pytest_cache .ruff_cache build dist src/elt_taskgen.egg-info constraints.txt
	find src tests tools -name __pycache__ -type d -exec rm -rf {} +
