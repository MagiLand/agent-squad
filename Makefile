PYTHON ?= python3

.PHONY: test
test:
	PYTHONPATH="$(CURDIR)/src" $(PYTHON) -m unittest discover -s tests
