.PHONY: help install run test lint format demo

VENV := .venv
PYTHON := $(VENV)/bin/python
PYTEST := $(VENV)/bin/pytest
RUFF := $(VENV)/bin/ruff

# Fallback to Windows virtualenv paths if .venv/bin doesn't exist
ifeq ($(OS),Windows_NT)
	PYTHON := $(VENV)/Scripts/python.exe
	PYTEST := $(VENV)/Scripts/pytest.exe
	RUFF := $(VENV)/Scripts/ruff.exe
endif

help:
	@echo "Available commands:"
	@echo "  make install  - Install requirements in virtualenv"
	@echo "  make run      - Run API Shield Flask application"
	@echo "  make test     - Run pytest suite with coverage"
	@echo "  make lint     - Run ruff linter and format checker"
	@echo "  make format   - Run ruff auto-fix and formatting"
	@echo "  make demo     - Execute end-to-end demo script"

install:
	python3 -m venv $(VENV) || python -m venv $(VENV)
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements-dev.txt

run:
	$(PYTHON) app.py

test:
	$(PYTEST) --cov=. --cov-report=term-missing

lint:
	$(RUFF) check .
	$(RUFF) format --check .

format:
	$(RUFF) check --fix .
	$(RUFF) format .

demo:
	$(PYTHON) scripts/demo.py
