#!/usr/bin/env bash
# Download Copernicus GLO-30 DEM tile(s) covering a lat/lon into cache/dem/.
# Free, no API key. Requires awscli OR curl. Copernicus tiles are 1x1 degree.
#
# Usage:  ./scripts/download_dem.sh <lat> <lon>
# Example: ./scripts/download_dem.sh 45 6
#
# After placing .tif tiles in cache/dem/, install rasterio to enable fast,
# accurate LOCAL elevation sampling (no rate limits):
#     .venv/bin/pip install rasterio
set -euo pipefail

LAT=${1:?"lat required (integer floor of your latitude, e.g. 45)"}
LON=${2:?"lon required (integer floor of your longitude, e.g. 6)"}
DEST="$(cd "$(dirname "$0")/.." && pwd)/cache/dem"
mkdir -p "$DEST"

fmt() { # zero-pad, add hemisphere letter
  local v=$1 pos=$2 neg=$3 width=$4
  if [ "$v" -lt 0 ]; then printf "%s%0${width}d" "$neg" "$(( -v ))"
  else printf "%s%0${width}d" "$pos" "$v"; fi
}
NS=$(fmt "$LAT" N S 2)
EW=$(fmt "$LON" E W 3)
TILE="Copernicus_DSM_COG_10_${NS}_00_${EW}_00_DEM"
KEY="${TILE}/${TILE}.tif"
URL="https://copernicus-dem-30m.s3.amazonaws.com/${KEY}"

echo "Downloading $URL"
curl -fSL "$URL" -o "$DEST/${TILE}.tif"
echo "Saved to $DEST/${TILE}.tif"
echo "Install rasterio to use it: .venv/bin/pip install rasterio"
