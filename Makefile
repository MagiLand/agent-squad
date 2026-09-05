PYTHON ?= python3

.PHONY: test smoke doctor
test:
	PYTHONPATH="$(CURDIR)/src" $(PYTHON) -m unittest discover -s tests

smoke:
	$(PYTHON) scripts/run-smoke-tests

doctor:
	PYTHONPATH="$(CURDIR)/src" $(PYTHON) -m agent_squad doctor
