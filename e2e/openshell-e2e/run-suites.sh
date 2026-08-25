#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Suite runner for the openshell-e2e container image. Invoked by
# with-kube-gateway.sh after the gateway is deployed and port-forwarded.
#
# Usage: run-suites.sh [suite] [test-filter]
#   suite:       e2e-kubernetes | e2e-python (default: both)
#   test-filter: optional test name filter
#
# At this point the gateway is running and the following env vars are set
# by with-kube-gateway.sh:
#   OPENSHELL_GATEWAY, OPENSHELL_E2E_DRIVER, OPENSHELL_E2E_HEALTH_PORT,
#   OPENSHELL_E2E_SANDBOX_NAMESPACE, XDG_CONFIG_HOME

set -euo pipefail

APP_DIR="/app"
RESULTS_DIR="${RESULTS_DIR:-/results}"
SUITE_FAILURES=0

log() { echo "==> $*"; }

# ── Parse arguments ─────────────────────────────────────────────────────────

SUITE="${1:-}"
TEST_FILTER="${2:-}"

if [ -z "${SUITE}" ]; then
  SUITES_TO_RUN=(e2e-kubernetes e2e-python)
else
  SUITES_TO_RUN=("${SUITE}")
fi

# ── Suite runners ───────────────────────────────────────────────────────────

run_e2e_kubernetes() {
  local filter="${1:-}"
  log "Running Rust Kubernetes E2E tests"

  local nextest_args=(
    cargo-nextest nextest run
    --archive-file "${APP_DIR}/e2e-tests.tar.zst"
    --profile ci
    --workspace-remap "${APP_DIR}/e2e-source"
  )

  if [ -n "${filter}" ]; then
    log "Test filter: ${filter}"
    nextest_args+=(--filterset "binary(${filter})")
  fi

  local suite_exit=0
  "${nextest_args[@]}" || suite_exit=$?

  if [ -f "${APP_DIR}/e2e-source/target/nextest/ci/xunit_report.xml" ]; then
    cp "${APP_DIR}/e2e-source/target/nextest/ci/xunit_report.xml" \
      "${RESULTS_DIR}/rust-e2e-xunit.xml"
    log "Rust E2E JUnit report: ${RESULTS_DIR}/rust-e2e-xunit.xml"
  fi

  return "${suite_exit}"
}

run_e2e_python() {
  local filter="${1:-}"
  log "Running Python SDK E2E tests"

  local pytest_args=(
    uv run --directory "${APP_DIR}" pytest
    --junitxml="${RESULTS_DIR}/python-e2e-xunit.xml"
    -o "junit_suite_name=python-e2e"
    "${APP_DIR}/e2e/python/"
  )

  if [ -n "${filter}" ]; then
    log "Test filter: ${filter}"
    pytest_args+=(-k "${filter}")
  fi

  local suite_exit=0
  "${pytest_args[@]}" || suite_exit=$?
  log "Python E2E JUnit report: ${RESULTS_DIR}/python-e2e-xunit.xml"
  return "${suite_exit}"
}

# ── Run selected suites ────────────────────────────────────────────────────

log "Selected suites: ${SUITES_TO_RUN[*]}"

for suite in "${SUITES_TO_RUN[@]}"; do
  log "--- Suite: ${suite} ---"
  suite_exit=0

  case "${suite}" in
    e2e-kubernetes) run_e2e_kubernetes "${TEST_FILTER}" || suite_exit=$? ;;
    e2e-python)     run_e2e_python "${TEST_FILTER}"     || suite_exit=$? ;;
  esac

  if [ "${suite_exit}" -ne 0 ]; then
    log "FAIL: ${suite} (exit ${suite_exit})"
    SUITE_FAILURES=$((SUITE_FAILURES + 1))
  else
    log "PASS: ${suite}"
  fi
done

log "--- Results ---"
log "Suites run: ${#SUITES_TO_RUN[@]}, Failures: ${SUITE_FAILURES}"
ls -la "${RESULTS_DIR}"/*.xml 2>/dev/null || log "(no xunit files produced)"

if [ "${SUITE_FAILURES}" -gt 0 ]; then
  exit 1
fi
