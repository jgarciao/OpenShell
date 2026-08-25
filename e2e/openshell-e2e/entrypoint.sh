#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Entrypoint for the openshell-e2e container image. Validates prerequisites,
# detects OpenShift clusters, and delegates to with-kube-gateway.sh which
# handles the full gateway lifecycle (deploy, port-forward, run, cleanup).
#
# Usage: entrypoint.sh [suite] [test-filter]
#   Suites: e2e-kubernetes, e2e-python
#   Defaults to all suites if no argument is given.
#
# Examples:
#   entrypoint.sh                           # all suites
#   entrypoint.sh e2e-kubernetes            # all Rust Kubernetes tests
#   entrypoint.sh e2e-kubernetes smoke      # only the smoke test binary
#   entrypoint.sh e2e-python                # all Python SDK tests
#   entrypoint.sh e2e-python test_sandbox   # Python tests matching "test_sandbox"

set -euo pipefail

APP_DIR="/app"

KUBECONFIG="${KUBECONFIG:-/home/runner/.kube/config}"
export KUBECONFIG

NAMESPACE="openshell"
RESULTS_DIR="${RESULTS_DIR:-/results}"
OPENSHELL_BIN="${OPENSHELL_BIN:-/usr/local/bin/openshell}"

export OPENSHELL_BIN
export OPENSHELL_TELEMETRY_ENABLED="${OPENSHELL_TELEMETRY_ENABLED:-false}"

log() { echo "==> $*"; }

# ── Validate arguments ──────────────────────────────────────────────────────

VALID_SUITES=(e2e-kubernetes e2e-python)

if [ "$#" -gt 0 ]; then
  suite="$1"
  valid=false
  for s in "${VALID_SUITES[@]}"; do
    if [ "${suite}" = "${s}" ]; then valid=true; break; fi
  done
  if [ "${valid}" = "false" ]; then
    echo "ERROR: Unknown suite '${suite}'. Valid: ${VALID_SUITES[*]}" >&2
    exit 2
  fi
fi

# ── Validate prerequisites ──────────────────────────────────────────────────

if [ ! -f "${KUBECONFIG}" ]; then
  echo "ERROR: KUBECONFIG not found at ${KUBECONFIG}" >&2
  echo "Mount it: -v ~/.kube/config:/home/runner/.kube/config:z,ro" >&2
  exit 2
fi

for cmd in kubectl helm; do
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "ERROR: ${cmd} not found in PATH" >&2
    exit 2
  fi
done

if [ ! -x "${OPENSHELL_BIN}" ]; then
  echo "ERROR: openshell CLI not found at ${OPENSHELL_BIN}" >&2
  exit 2
fi

mkdir -p "${RESULTS_DIR}" 2>/dev/null || true
if [ ! -w "${RESULTS_DIR}" ]; then
  RESULTS_DIR="/tmp/openshell-e2e-results"
  mkdir -p "${RESULTS_DIR}"
  log "WARNING: Mounted results dir not writable, using ${RESULTS_DIR}"
fi
export RESULTS_DIR

# ── Detect OpenShift and apply SCC ──────────────────────────────────────────

IS_OPENSHIFT=false
if command -v oc >/dev/null 2>&1 \
   && oc api-versions 2>/dev/null | grep -q security.openshift.io; then
  IS_OPENSHIFT=true
  log "Detected OpenShift cluster"

  kubectl create namespace "${NAMESPACE}" 2>/dev/null || true
  log "Applying OpenShift SCC bindings"
  oc adm policy add-scc-to-user privileged \
    -z openshell-sandbox -n "${NAMESPACE}"

  OPENSHELL_E2E_KUBE_EXTRA_VALUES="${OPENSHELL_E2E_KUBE_EXTRA_VALUES:+${OPENSHELL_E2E_KUBE_EXTRA_VALUES}:}deploy/helm/openshell/ci/values-openshift-e2e.yaml"
  export OPENSHELL_E2E_KUBE_EXTRA_VALUES
else
  log "Detected vanilla Kubernetes cluster"
fi

cleanup_openshift_scc() {
  if [ "${IS_OPENSHIFT}" = "true" ]; then
    log "Removing OpenShift SCC bindings"
    oc adm policy remove-scc-from-user privileged \
      -z openshell-sandbox -n "${NAMESPACE}" 2>/dev/null || true
  fi
}
trap cleanup_openshift_scc EXIT

# ── Set context and delegate to with-kube-gateway.sh ────────────────────────

export OPENSHELL_E2E_KUBE_CONTEXT
OPENSHELL_E2E_KUBE_CONTEXT="$(kubectl config current-context)"

log "Using kube context: ${OPENSHELL_E2E_KUBE_CONTEXT}"
log "Results directory: ${RESULTS_DIR}"

exec "${APP_DIR}/e2e/with-kube-gateway.sh" \
  "${APP_DIR}/e2e/openshell-e2e/run-suites.sh" "$@"
