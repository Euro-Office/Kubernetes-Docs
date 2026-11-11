#!/usr/bin/env bash

set -euo pipefail

chart_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
rendered_manifest="$(mktemp)"
trap 'rm -f "${rendered_manifest}"' EXIT

helm template docs "${chart_dir}" --set upgrade.job.enabled=true > "${rendered_manifest}"

job_manifest="$(awk '
  /^# Source: docs\/templates\/jobs\/pre-upgrade.yaml$/ { in_manifest=1 }
  in_manifest { print }
  in_manifest && /^---$/ { exit }
' "${rendered_manifest}")"

configmap_manifest="$(awk '
  /^# Source: docs\/templates\/configmaps\/documentserver.yaml$/ { in_manifest=1 }
  in_manifest { print }
  in_manifest && /^---$/ { exit }
' "${rendered_manifest}")"

grep -Fq '"helm.sh/hook": pre-upgrade' <<<"${job_manifest}"
grep -Fq 'name: documentserver' <<<"${configmap_manifest}"
grep -Fq 'APPLICATION_NAME:' <<<"${configmap_manifest}"
grep -Fq 'name: documentserver' <<<"${job_manifest}"
grep -Fq 'optional: true' <<<"${job_manifest}"
grep -Fq 'mountPath: /documentserver-config' <<<"${job_manifest}"
grep -Fq 'if [ ! -f /documentserver-config/APPLICATION_NAME ]; then' <<<"${job_manifest}"
grep -Fq 'exit 0' <<<"${job_manifest}"

echo "pre-upgrade hook rendering checks passed"
