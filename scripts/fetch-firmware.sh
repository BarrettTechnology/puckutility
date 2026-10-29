#!/usr/bin/env bash
# Fill firmware/ with the puck firmware the release ships: the stm32 project's
# tag-pipeline `release-job` artifacts (*.bin / *.ebin). Then check that every
# `fw =` file system-config.ini names is there, so a release cannot ship a
# config that points at missing firmware.
#
#   FIRMWARE_TAG   stm32 tag to take (default: v<fw_version> from the
#                  [DEFAULT] section of system-config.ini)
#   STM32_PROJECT  GitLab project path (default: firmware/stm32)
#
# Auth: in CI the job token (CI_JOB_TOKEN; the stm32 project must allow this
# project under Settings > CI/CD > Job token permissions). Elsewhere set
# GITLAB_TOKEN to a personal access token with read_api.
set -euo pipefail
cd "$(dirname "$0")/.."

API="${CI_API_V4_URL:-https://git.barrett.com/api/v4}"
PROJECT="${STM32_PROJECT:-firmware/stm32}"
if [ -z "${FIRMWARE_TAG:-}" ]; then
    FW_VERSION=$(awk -F' *= *' '/^\[/{s=$0} s=="[DEFAULT]" && $1=="fw_version"{print $2; exit}' system-config.ini)
    [ -n "$FW_VERSION" ] || { echo "ERROR: no fw_version in [DEFAULT] of system-config.ini; set FIRMWARE_TAG" >&2; exit 1; }
    FIRMWARE_TAG="v${FW_VERSION}"
fi
if [ -n "${CI_JOB_TOKEN:-}" ]; then
    AUTH="JOB-TOKEN: ${CI_JOB_TOKEN}"
elif [ -n "${GITLAB_TOKEN:-}" ]; then
    AUTH="PRIVATE-TOKEN: ${GITLAB_TOKEN}"
else
    echo "ERROR: set GITLAB_TOKEN (read_api) to download the firmware" >&2; exit 1
fi

ZIP="$(mktemp --suffix=.zip)"
trap 'rm -f "$ZIP"' EXIT
URL="${API}/projects/${PROJECT//\//%2F}/jobs/artifacts/${FIRMWARE_TAG}/download?job=release-job"
echo "Fetching ${PROJECT} ${FIRMWARE_TAG} release-job artifacts"
curl -fsSL --header "$AUTH" -o "$ZIP" "$URL" || {
    echo "ERROR: download failed: ${URL}" >&2; exit 1; }
mkdir -p firmware
unzip -o -j -q "$ZIP" '*.bin' '*.ebin' -d firmware/ || true
ls firmware/*.bin firmware/*.ebin >/dev/null 2>&1 || {
    echo "ERROR: ${FIRMWARE_TAG} release-job has no .bin/.ebin artifacts" >&2; exit 1; }
ls -l firmware/

missing=0
for fw in $(awk -F' *= *' '$1=="fw"{print $2}' system-config.ini | sort -u); do
    [ -f "firmware/$(basename "$fw")" ] || { echo "ERROR: system-config.ini names $fw, not in ${FIRMWARE_TAG}'s artifacts" >&2; missing=1; }
done
exit "$missing"
