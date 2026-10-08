PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

# A PYTHON path puts its directory first on PATH, so the fake tools'
# "#!/usr/bin/env python3" resolves to that interpreter, not a shim.
TEST_PATH = $(if $(findstring /,$(PYTHON)),PATH="$(abspath $(dir $(PYTHON))):$$PATH" )

.PHONY: venv test smoke doctor
venv:
	@command -v uv >/dev/null 2>&1 || { echo "make venv needs uv, which is not on PATH" >&2; exit 1; }
	uv venv --clear --python 3.11 --seed .venv
	uv pip install --python .venv/bin/python 'setuptools>=77'

test:
	$(TEST_PATH)PYTHONPATH="$(CURDIR)/src" $(PYTHON) -m unittest discover -s tests

smoke:
	$(TEST_PATH)$(PYTHON) scripts/run-smoke-tests

doctor:
	PYTHONPATH="$(CURDIR)/src" $(PYTHON) -m agent_squad doctor
