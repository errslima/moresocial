#!/bin/sh
# Runs once when the Postgres data directory is first created. The application role is not a
# superuser; it owns the moresocial database so it can run the additive migrations.
set -eu
password="$(cat /run/secrets/db_password)"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v app_password="$password" <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE ROLE moresocial LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'app_password';
ALTER DATABASE moresocial OWNER TO moresocial;
ALTER SCHEMA public OWNER TO moresocial;
SQL
