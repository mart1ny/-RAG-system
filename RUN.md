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

## Поднятие API и фронта

1. **Backend-API (FastAPI + Uvicorn)**  
   ```bash
   source .venv/bin/activate
   pip install -r requirements.txt  # если ещё не поставлены зависимости
   CHAT_CHUNK_LIMIT=6 uvicorn scripts.api:app --reload --host 0.0.0.0 --port 8000
   ```
   API переиспользует подключение к Postgres/Qdrant, поэтому убедись, что инфраструктура и данные уже подняты/загружены.
   Переменная `CHAT_CHUNK_LIMIT` отвечает за количество фрагментов, которые вытягиваются и попадают в ответ (по умолчанию 6, можно увеличить до 8).

2. **Frontend (Vite + React)**  
   ```bash
   cd frontend
   npm install
   VITE_CHUNK_LIMIT=6 npm run dev
   ```
   По умолчанию UI ходит в `http://localhost:8000`. Чтобы указать другой адрес, прокинь переменную `VITE_API_URL`, например:
   ```bash
   VITE_API_URL="http://localhost:8080" npm run dev
   ```

3. **Продакшн-сборка фронта**  
   ```bash
   cd frontend
   npm run build
   npm run preview   # опционально посмотреть статическую сборку
   ```

После запуска `uvicorn …` и `npm run dev` открой `http://localhost:5173` и используй чат в интерфейсе, похожем на GPT: есть переключатель темы, подсветка источников и быстрая кнопка с примером запроса.

## Частые проблемы

- **`permission denied for table assignments`**  
  Значит, Postgres поднялся со старым томом, где таблицы принадлежат другому пользователю. Решение: `docker compose down -v && docker compose up -d`, затем повторить шаг 4.

- **Скрипт подключается к не тому Postgres**  
  Если в системе есть собственный Postgres на 5432, `scripts.ingest` может стучаться в него. Останови локальный сервис (`brew services stop postgresql@…`) или поменяй порт публикации контейнера и переменную `POSTGRES_DSN`.

- **`ModuleNotFoundError: No module named 'scripts'`**  
  Запускай скрипты как модули: `python -m scripts.ingest`, `python -m scripts.search`.

Следуя этим шагам, получаешь рабочий локальный стек с наполненной векторной базой Qdrant и связанными данными в PostgreSQL/MongoDB/Redis/Neo4j.
