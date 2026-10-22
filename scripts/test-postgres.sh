#!/usr/bin/env sh
# Run the backend suite, including PostgreSQL-only tests, against a disposable
# database inside the Compose stack. The development database is never touched.
set -eu

DB_NAME="${TEST_DB_NAME:-beework_test}"
cd "$(dirname "$0")/.."

docker compose exec -T postgres sh -c "
  psql -U \"\$POSTGRES_USER\" -d postgres -v ON_ERROR_STOP=1 \
    -c 'DROP DATABASE IF EXISTS \"$DB_NAME\" WITH (FORCE)' \
    -c 'CREATE DATABASE \"$DB_NAME\"'
"

TEST_URL="postgresql+psycopg://beework:beework_local_only@postgres:5432/$DB_NAME"
docker compose exec -T \
  -e DATABASE_URL="$TEST_URL" \
  -e TEST_DATABASE_URL="$TEST_URL" \
  api sh -c "alembic upgrade head && alembic check && alembic downgrade base && alembic upgrade head && python -m unittest discover -s tests -v"
