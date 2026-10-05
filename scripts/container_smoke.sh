#!/bin/sh
# Isolated local test; never reads production environment files or databases.
set -eu
umask 077
smoke_env=$(mktemp)
cleanup() {
  docker compose -p open-alvary-ci --env-file "$smoke_env" -f deployment/compose.yaml -f deployment/smoke.yaml down -v
  rm -f "$smoke_env"
}
trap cleanup EXIT INT TERM
cat > "$smoke_env" <<CONFIG
PUBLIC_DOMAIN=pilot.example.org
PUBLIC_REPOSITORY_URL=https://github.com/example/open-alvary
PUBLIC_MAINTAINER=Synthetic CI maintainer
PUBLIC_CONTACT_EMAIL=test@example.org
METADATA_LICENCE=pending
POSTGRES_PASSWORD=$(openssl rand -hex 32)
CONFIG
docker compose -p open-alvary-ci --env-file "$smoke_env" -f deployment/compose.yaml -f deployment/smoke.yaml up --build -d --wait web
python -m scripts.smoke_public http://127.0.0.1:5179
