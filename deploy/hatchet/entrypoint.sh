#!/bin/sh
set -eu
# Do not set -x or put secret values into compose, arguments or logs.
DATABASE_POSTGRES_PASSWORD=$(cat /run/secrets/database_password)
ADMIN_PASSWORD=$(cat /run/secrets/administrator_password)
case "$DATABASE_POSTGRES_PASSWORD" in
  ''|*[!A-Za-z0-9_-]*) echo 'database_password_requires_random_url_safe_characters' >&2; exit 1 ;;
esac
# The pinned migration binary requires DATABASE_URL even though the server
# supports discrete PostgreSQL fields. Never echo the resulting secret DSN.
DATABASE_URL="postgresql://${DATABASE_POSTGRES_USERNAME}:${DATABASE_POSTGRES_PASSWORD}@${DATABASE_POSTGRES_HOST}:${DATABASE_POSTGRES_PORT}/${DATABASE_POSTGRES_DB_NAME}?sslmode=disable"
export DATABASE_POSTGRES_PASSWORD DATABASE_URL ADMIN_PASSWORD
exec /entrypoint.sh
