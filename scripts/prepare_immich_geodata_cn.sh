#!/usr/bin/env bash
set -euo pipefail

release_tag="${IMMICH_GEODATA_CN_RELEASE:-auto-release}"
geodata_asset="${1:-geodata_admin_2_admin_3_full.zip}"
proxy_prefix="${IMMICH_GEODATA_CN_GITHUB_PROXY:-https://gh-proxy.com/}"
direct_base_url="https://github.com/ZingLix/immich-geodata-cn/releases/download/${release_tag}"
if [[ "$proxy_prefix" == "direct" ]]; then
  base_url="$direct_base_url"
else
  base_url="${proxy_prefix%/}/${direct_base_url}"
fi
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target="${root}/immich/geodata-cn"
temp="$(mktemp -d)"
trap 'rm -rf "$temp"' EXIT

mkdir -p "${target}"
curl --fail --http1.1 --location --retry 3 --retry-all-errors --retry-delay 2 \
  --connect-timeout 30 --max-time 600 \
  --output "${temp}/geodata.zip" "${base_url}/${geodata_asset}"
curl --fail --http1.1 --location --retry 3 --retry-all-errors --retry-delay 2 \
  --connect-timeout 30 --max-time 600 \
  --output "${temp}/i18n-iso-countries.zip" "${base_url}/i18n-iso-countries.zip"

mkdir "${temp}/geodata" "${temp}/i18n"
unzip -q "${temp}/geodata.zip" -d "${temp}/geodata"
unzip -q "${temp}/i18n-iso-countries.zip" -d "${temp}/i18n"

geodata_root="${temp}/geodata/geodata"
test -f "${geodata_root}/geodata-date.txt"
test -d "${temp}/i18n/i18n-iso-countries/langs"

rm -rf "${target}/geodata" "${target}/i18n-iso-countries"
mv "${geodata_root}" "${target}/geodata"
mv "${temp}/i18n/i18n-iso-countries" "${target}/i18n-iso-countries"
echo "Installed ${geodata_asset} from ${release_tag}."
