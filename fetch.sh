#!/usr/bin/env bash
# Download a pinned otelcol-contrib, verify its published sha256, and install the
# linux/amd64 binary next to config.yaml for `cf push`. Also keeps a native
# binary under .dl/ for local `validate`.
set -euo pipefail

VERSION="${OTELCOL_VERSION:-0.164.0}"
BASE="https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${VERSION}"
HERE="$(cd "$(dirname "$0")" && pwd)"
DL="$HERE/.dl"
mkdir -p "$DL"

fetch() { # <platform> <dest-dir>
  local plat="$1" dest="$2" f="otelcol-contrib_${VERSION}_$1.tar.gz"
  [ -f "$DL/$f" ] || curl -fsSL -o "$DL/$f" "$BASE/$f"
  curl -fsSL -o "$DL/$f.sha256" "$BASE/$f.sha256"
  local want got
  want="$(tr -d '[:space:]' < "$DL/$f.sha256")"
  got="$(shasum -a 256 "$DL/$f" | awk '{print $1}')"
  [ "$want" = "$got" ] || { echo "sha256 mismatch for $f" >&2; exit 1; }
  mkdir -p "$dest"
  tar -xzf "$DL/$f" -C "$dest" otelcol-contrib
  echo "ok  $f"
}

fetch linux_amd64 "$HERE"
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64) fetch darwin_arm64 "$DL/native" ;;
  Darwin-x86_64) fetch darwin_amd64 "$DL/native" ;;
  Linux-x86_64) cp "$HERE/otelcol-contrib" "$DL/native-otelcol-contrib" ;;
esac
