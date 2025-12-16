# Local Run Guide

## Prerequisites

- Docker Desktop (или совместимый демон Docker) — нужен для поднятия Postgres, MongoDB, Redis, Qdrant и Neo4j из `docker-compose.yml`.
- Python 3.9+ с установленными Xcode Command Line Tools (для корректной работы `pip` и brew).
- Свободный порт 5432. Если на машине уже работает собственный Postgres (например, установленный через Homebrew), останови его командой `brew services stop postgresql@<версия>` или измени порт в `docker-compose.yml` (например, `15432:5432`).

## Первый запуск

```bash
# 1. Очистить (если запускал ранее) и поднять инфраструктуру
docker compose down -v        # удаляет контейнеры и тома, чтобы Postgres пересоздал схему
docker compose up -d

# 2. Создать виртуальное окружение и поставить зависимости
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. (Опционально) указать свои DSN
export POSTGRES_DSN="postgresql://rag:ragpass@localhost:5432/rag"
export MONGODB_URI="mongodb://localhost:27017"
export REDIS_URL="redis://localhost:6379/0"
export QDRANT_HOST="localhost"
export QDRANT_PORT="6333"

# 4. Загрузить данные из data/materials.json
python -m scripts.ingest

# 5. Проверить поиск
python -m scripts.search "пайплайн RAG"
```

## Частые проблемы

- **`permission denied for table assignments`**  
  Значит, Postgres поднялся со старым томом, где таблицы принадлежат другому пользователю. Решение: `docker compose down -v && docker compose up -d`, затем повторить шаг 4.

- **Скрипт подключается к не тому Postgres**  
  Если в системе есть собственный Postgres на 5432, `scripts.ingest` может стучаться в него. Останови локальный сервис (`brew services stop postgresql@…`) или поменяй порт публикации контейнера и переменную `POSTGRES_DSN`.

- **`ModuleNotFoundError: No module named 'scripts'`**  
  Запускай скрипты как модули: `python -m scripts.ingest`, `python -m scripts.search`.

Следуя этим шагам, получаешь рабочий локальный стек с наполненной векторной базой Qdrant и связанными данными в PostgreSQL/MongoDB/Redis/Neo4j.
