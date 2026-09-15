#!/usr/bin/env bash
#
# Deploiement de pieces. A lancer sur le serveur, depuis le dossier du depot.
#
#   ./deploy.sh

set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "Il manque un .env. Copie .env.example et renseigne les valeurs." >&2
  exit 1
fi

echo "==> git pull"
git pull --ff-only

echo "==> docker compose up"
docker compose up -d --build --remove-orphans

echo "==> etat"
docker compose ps
