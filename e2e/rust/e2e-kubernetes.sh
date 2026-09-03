#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Run the Rust e2e suite against an OpenShell gateway deployed on Kubernetes
# via Helm. Set OPENSHELL_E2E_KUBE_CONTEXT to target an existing cluster;
# otherwise an ephemeral k3d cluster is created and torn down by
# with-kube-gateway.sh. Set OPENSHELL_E2E_KUBE_TEST to scope to a single
# integration test for local debugging.
#
# Features: the default set includes `e2e-host-gateway` so tests that rely on
# the sandbox-side `host.openshell.internal` alias compile and run. The
# wrapper detects the cluster's host-routable IP and wires it into the chart
# via `server.hostGatewayIP`. Targeting a cluster where the test host is
# unreachable from pods? Set OPENSHELL_E2E_KUBERNETES_FEATURES=e2e to drop the
# alias-dependent tests entirely.
#
# Results: the `e2e-kubernetes` nextest profile writes a JUnit report to
# results/e2e-kubernetes.xml at the repo root (see .config/nextest.toml). After
# each run, run_suite renders that XML to a sibling .html via xsltproc and
# scripts/junit-to-html.xsl (best-effort — a rendering failure never masks the
# test exit code, and the report is produced on failure too). When running both
# credential drivers, run_suite renames the report per driver (e.g.
# results/e2e-kubernetes-vault.xml) so the runs don't clobber each other.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_WITH_GATEWAY_COMMAND="__openshell_run_kubernetes_e2e"
# shellcheck source=e2e/support/conformance.sh
source "${ROOT}/e2e/support/conformance.sh"

E2E_FEATURES="${OPENSHELL_E2E_KUBERNETES_FEATURES-e2e,e2e-host-gateway,e2e-kubernetes}"

# JUnit report written by the `e2e-kubernetes` nextest profile (its junit.path
# lands here because run_e2e pins --target-dir). The .xml is the profile's fixed
# output; per-driver runs rename it before rendering so they don't clobber it.
JUNIT_XML="${ROOT}/results/e2e-kubernetes.xml"

# Render a JUnit XML to a standalone HTML report next to it (same basename).
# Best-effort: a missing xsltproc or a rendering error only warns, so it never
# masks the test result.
render_html() {
  local xml="${1:-${JUNIT_XML}}"
  [ -f "${xml}" ] || return 0
  local html="${xml%.xml}.html"
  if command -v xsltproc >/dev/null 2>&1; then
    xsltproc --stringparam title "e2e-kubernetes test report" \
      "${ROOT}/scripts/junit-to-html.xsl" "${xml}" >"${html}" \
      && echo "HTML report: ${html}" \
      || echo "WARNING: failed to render HTML report from ${xml}" >&2
  else
    echo "WARNING: xsltproc not found; skipping HTML report (${xml} still written)" >&2
  fi
}

# Docker and Podman build their local gateway and CLI together in the shared
# gateway wrapper. Kubernetes consumes published gateway images, so only its
# local CLI needs to be built when CI has not supplied a prebuilt one.
if [ -z "${OPENSHELL_BIN:-}" ]; then
  cargo build -p openshell-cli
  export OPENSHELL_BIN="${ROOT}/target/debug/openshell"
fi

test_filter=()
if [ -n "${OPENSHELL_E2E_KUBE_TEST:-}" ]; then
  test_filter+=(--test "${OPENSHELL_E2E_KUBE_TEST}")
fi

is_operator_workspace_mode() {
  [[ ",${E2E_FEATURES}," == *",e2e-kubernetes-workspace-operator,"* ]]
}

run_conformance() {
  if is_operator_workspace_mode; then
    echo "note: skipping standalone CLI conformance in Kubernetes operator workspace mode; default workspace is not operator-allowlisted (see #2971)."
    return 0
  fi

  e2e_run_openshell_conformance "Kubernetes"
}

# Run the suite and collect the report. When $1 differs from the default
# JUNIT_XML path, rename the fixed nextest output so per-driver reports
# don't clobber each other. The real test exit code is returned after
# report collection so callers (and set -e) see the right status.
run_suite() {
  local report="${1:-${JUNIT_XML}}"
  local status=0
  "${ROOT}/e2e/with-kube-gateway.sh" \
    bash "${BASH_SOURCE[0]}" "${RUN_WITH_GATEWAY_COMMAND}" || status=$?
  if [ "${report}" != "${JUNIT_XML}" ]; then
    mv -f "${JUNIT_XML}" "${report}" 2>/dev/null || true
  fi
  render_html "${report}"
  return "${status}"
}

run_e2e() {
  run_conformance
  if [ -z "${E2E_FEATURES}" ]; then
    return 0
  fi

  # Pin --target-dir so the profile store dir (and thus junit.path) resolves to
  # e2e/rust/target regardless of any inherited CARGO_TARGET_DIR.
  cargo nextest run --profile e2e-kubernetes \
    --config-file "${ROOT}/.config/nextest.toml" \
    --target-dir "${ROOT}/e2e/rust/target" \
    --manifest-path "${ROOT}/e2e/rust/Cargo.toml" \
    --features "${E2E_FEATURES}" \
    ${test_filter[@]+"${test_filter[@]}"}
}

if [ "${1:-}" = "${RUN_WITH_GATEWAY_COMMAND}" ]; then
  run_e2e
  exit 0
fi

# Multi-driver mode: run once per credential-storage backend (kubernetes-secrets
# and vault). Each backend is wired via a Helm values overlay in with-kube-gateway.sh;
# vault additionally deploys a dev OpenBao instance. Default runs without either.
if [ "${OPENSHELL_E2E_CREDENTIAL_DRIVERS:-0}" = "1" ] \
   && [ -z "${OPENSHELL_E2E_CREDENTIAL_DRIVER:-}" ]; then
  OPENSHELL_E2E_CREDENTIAL_DRIVER=kubernetes-secrets \
    run_suite "${ROOT}/results/e2e-kubernetes-secrets.xml"
  OPENSHELL_E2E_CREDENTIAL_DRIVER=vault \
    run_suite "${ROOT}/results/e2e-kubernetes-vault.xml"
else
  run_suite
fi
