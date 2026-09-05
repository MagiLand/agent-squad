PYTHON ?= python3

.PHONY: test doctor
test:
	PYTHONPATH="$(CURDIR)/src" $(PYTHON) -m unittest discover -s tests

doctor:
	PYTHONPATH="$(CURDIR)/src" $(PYTHON) -m agent_squad doctor
