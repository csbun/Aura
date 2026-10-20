#!/usr/bin/env sh
set -eu

# Run from the directory containing docker-compose.yml. Put this in the
# sidecar host's root crontab, after mounting the NAS SMB share:
#   0 2 * * * /opt/aura/scripts/nightly-scan.sh >>/var/log/aura-scan.log 2>&1
exec docker compose --profile jobs run --rm aura-scan
