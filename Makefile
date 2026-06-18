.PHONY: help dev docker-build docker-up docker-down docker-restart docker-logs docker-ps docker-shell docker-health docker-config

help:
	@echo "CV Adapter commands"
	@echo ""
	@echo "Local development:"
	@echo "  make dev             Run the app locally with uvicorn reload"
	@echo ""
	@echo "Docker:"
	@echo "  make docker-build    Build the Docker image"
	@echo "  make docker-up       Build and start the app container"
	@echo "  make docker-down     Stop and remove the app container"
	@echo "  make docker-restart  Restart the app container"
	@echo "  make docker-logs     Follow app container logs"
	@echo "  make docker-ps       Show Compose service status"
	@echo "  make docker-shell    Open a shell inside the app container"
	@echo "  make docker-health   Call the app health endpoint from inside the container"
	@echo "  make docker-config   Show Compose service names without expanding secrets"

dev:
	python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000

docker-build:
	docker compose build

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

docker-restart:
	docker compose restart app

docker-logs:
	docker compose logs -f app

docker-ps:
	docker compose ps

docker-shell:
	docker compose exec app sh

docker-health:
	docker compose exec app python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read().decode())"

docker-config:
	docker compose config --services
