# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# Coffer — Makefile
.PHONY: install dev test lint typecheck security ci commit clean

install:
	pip install -e ".[dev]"

dev:
	uvicorn --factory coffer._engine.config:create_app --reload --host 0.0.0.0 --port 8001

test:
	pytest tests/ -v

lint:
	ruff check . && ruff format --check .

typecheck:
	pyright coffer/ || mypy coffer/

security:
	bandit -r coffer/ -q || true
	gitleaks detect --no-git || true

ci: lint typecheck test

commit:
	@git add -A && git commit -m "$$(git commit --dry-run 2>&1 | head -1)" 2>/dev/null || echo "Use: git commit -m 'type(scope): description'"

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache dist build *.egg-info
