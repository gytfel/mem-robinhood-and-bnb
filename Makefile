.PHONY: install setup run doctor test lint fmt key docker clean

install:            ## поставить зависимости и команду sniper в .venv
	python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/pip install -e .

run:                ## запустить бота
	.venv/bin/sniper run

doctor:             ## проверить конфигурацию, Telegram и RPC
	.venv/bin/sniper doctor

setup:              ## создать .env и ключи
	.venv/bin/sniper init

test:               ## прогнать тесты
	.venv/bin/python -m pytest -q

lint:               ## проверить стиль
	.venv/bin/ruff check .

fmt:                ## починить стиль автоматически
	.venv/bin/ruff check . --fix

key:                ## сгенерировать MASTER_KEY
	@python3 -c "import secrets; print(secrets.token_urlsafe(48))"

docker:             ## собрать и запустить в docker
	docker compose up -d --build

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__
