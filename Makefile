.PHONY: install test lint build dev

install:
	python3 -m pip install -e './apps/api[dev]'
	cd apps/web && npm install

test:
	pytest apps/api/tests
	cd apps/web && npm test -- --run

lint:
	ruff check apps/api
	cd apps/web && npm run lint

build:
	cd apps/web && npm run build

dev:
	uvicorn app.main:app --app-dir apps/api --reload
