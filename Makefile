.PHONY: test
PYTHON ?= .venv/bin/python
test:
	$(PYTHON) -m unittest discover -s tests -v
