#!/bin/sh
# Maps a bound splunk-o11y service instance onto the environment variables the
# Splunk OTel JS agent reads.
#
# Why this file exists: the Java buildpack reads VCAP_SERVICES itself, so a Java
# app needs no shim. The Node.js buildpack v4.13.0 does not -- there is no
# reference to VCAP_SERVICES anywhere in the buildpack or the bundled agent.
#
# Cloud Foundry sources this script at launch, after the buildpack's profile.d
# scripts, so node is already on PATH and NODE_OPTIONS already carries the agent
# preload. The variables are in place before the start command runs.
#
# Nothing is echoed: the exports are captured by the eval, so no token reaches
# the Cloud Foundry log stream.

if [ -n "${VCAP_SERVICES:-}" ] && [ -f "${HOME:-/home/vcap/app}/splunk-binding.js" ]; then
  eval "$(node "${HOME:-/home/vcap/app}/splunk-binding.js")"
fi
