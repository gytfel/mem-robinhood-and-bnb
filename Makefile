.PHONY: install run test lint fmt key docker clean

install:            ## поставить зависимости в .venv
	python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

run:                ## запустить бота
	.venv/bin/python -m sniperbot

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
