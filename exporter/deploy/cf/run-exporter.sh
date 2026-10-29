#!/usr/bin/env bash
# Web process: the SAP CF metrics exporter.
# Secrets arrive through the bound user-provided service (VCAP_SERVICES), never via files.
set -euo pipefail
cd "$(dirname "$0")/../.."

# The package is shipped as source (src/), not pip-installed. Set the path here rather than in the
# manifest: the python buildpack manages PYTHONPATH itself and the manifest value did not survive.
export PYTHONPATH="${PWD}/src${PYTHONPATH:+:${PYTHONPATH}}"

eval "$(python -m sap_cf_exporter.cf_env)"

exec python -m sap_cf_exporter
