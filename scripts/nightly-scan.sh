#!/usr/bin/env sh
set -eu

# Run from the repository directory. Default mode starts Aura with Immich's
# shared SMB volume; set AURA_COMPOSE_MODE=smb for Aura-only deployments.
case "${AURA_COMPOSE_MODE:-immich}" in
  immich) set -- -f docker-compose.yml -f docker-compose.immich.yml ;;
  smb) set -- -f docker-compose.yml -f docker-compose.smb.yml ;;
  *) echo "AURA_COMPOSE_MODE must be immich or smb" >&2; exit 2 ;;
esac
exec docker compose "$@" --profile jobs run --rm aura-scan
