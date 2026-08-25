# End-to-End Tests

Integration and end-to-end tests for OpenShell, organized by runtime driver and SDK.

## Prerequisites

- [mise](https://mise.jdx.dev/) task runner
- Rust toolchain (for Rust E2E compilation)
- Python 3.11+ and [uv](https://docs.astral.sh/uv/) (for Python E2E)
- Docker or Podman (for container-based compute drivers)
- kubectl and Helm 3.x (for Kubernetes/OpenShift tests)
- oc CLI (for OpenShift-specific tests)

## Test Suites

### Rust E2E (`e2e/rust/`)

The main integration test suite, covering sandbox lifecycle, exec, networking, policy enforcement, and multi-driver scenarios. Tests are gated behind Cargo feature flags.

| Task | Description |
|------|-------------|
| `mise run e2e:docker` | Docker compute driver |
| `mise run e2e:podman` | Podman compute driver |
| `mise run e2e:podman:rootless` | Rootless Podman |
| `mise run e2e:vm` | VM compute driver (libkrun) |
| `mise run e2e:kubernetes` | Kubernetes (k3d or existing cluster) |
| `mise run e2e:openshift` | OpenShift database-backend validation |
| `mise run e2e:docker:gpu` | Docker with GPU passthrough |
| `mise run e2e:podman:gpu` | Podman with GPU passthrough |
| `mise run e2e:docker:external-driver` | External Docker driver binary |
| `mise run e2e:podman:external-driver` | External Podman driver binary |
| `mise run e2e:vm:external-driver` | External VM driver binary |
| `mise run e2e:kubernetes:external-driver` | External Kubernetes driver sidecar |
| `mise run e2e:kubernetes:credential-drivers` | Kubernetes + Vault credential backends |
| `mise run e2e:kubernetes:workspace-managed` | Managed workspace mode |
| `mise run e2e:kubernetes:workspace-operator` | Operator workspace mode |
| `mise run e2e:kubernetes:db` | Multiple database backend scenarios |
| `mise run e2e:kubernetes:sidecar` | Sidecar injection |
| `mise run e2e:kubernetes:agent-sandbox-versions` | Cross-version CRD compatibility |
| `mise run e2e:gateway:no-compute-drivers` | Gateway without built-in drivers |

For Kubernetes tests, set `OPENSHELL_E2E_KUBE_CONTEXT` to target an existing cluster. Without it, an ephemeral k3d cluster is created and torn down automatically.

### Python E2E (`e2e/python/`)

Python SDK integration tests covering sandbox API, policy validation, exec, inference routing, TLS, and workspace management.

```shell
mise run e2e:python
```

Requires a running gateway. Uses `SandboxClient.from_active_cluster()` for connection.

### MCP Conformance (`e2e/mcp-conformance/`)

MCP protocol conformance tests against the OpenShell MCP bridge.

```shell
mise run e2e:mcp
```

### Policy Advisor (`e2e/policy-advisor/`)

Mechanistic smoke tests for the policy advisor flow.

```shell
mise run e2e:mechanistic-smoke
```

### GPU Tests (`e2e/gpu/`)

GPU passthrough validation for Docker and Podman drivers.

```shell
mise run e2e:gpu
```

## Custom Images

Tests that deploy a gateway via Helm accept custom image overrides through environment variables. The image reference can include a tag or digest:

```shell
# Tag reference
OPENSHELL_GATEWAY_IMAGE=quay.io/opendatahub/odh-openshell-gateway:odh-stable \
OPENSHELL_SUPERVISOR_IMAGE=quay.io/opendatahub/odh-openshell-supervisor:odh-stable \
OPENSHELL_BIN=/path/to/openshell \
mise run e2e:kubernetes

# Digest reference
OPENSHELL_GATEWAY_IMAGE=quay.io/opendatahub/odh-openshell-gateway@sha256:abc123... \
OPENSHELL_SUPERVISOR_IMAGE=quay.io/opendatahub/odh-openshell-supervisor@sha256:abc123... \
OPENSHELL_BIN=/path/to/openshell \
mise run e2e:kubernetes

# Separate repo and tag (backward compatible)
OPENSHELL_GATEWAY_IMAGE=quay.io/opendatahub/odh-openshell-gateway \
OPENSHELL_GATEWAY_TAG=odh-stable \
OPENSHELL_SUPERVISOR_IMAGE=quay.io/opendatahub/odh-openshell-supervisor \
OPENSHELL_SUPERVISOR_TAG=odh-stable \
OPENSHELL_BIN=/path/to/openshell \
mise run e2e:kubernetes
```

When unset, the defaults are `${OPENSHELL_REGISTRY}/gateway` and `${OPENSHELL_REGISTRY}/supervisor` with `${IMAGE_TAG}`.

## Container Image (`openshell-e2e`)

A self-contained test runner image for CI pipelines. Mounts a KUBECONFIG and results volume, auto-detects OpenShift vs vanilla Kubernetes, deploys a gateway via `with-kube-gateway.sh`, runs selected integration test suites, produces JUnit XML reports, and cleans up all cluster resources.

The image uses a configurable CLI base image (`quay.io/opendatahub/odh-openshell-cli:odh-stable` by default) and pre-compiled Rust test binaries via a nextest archive.

### Build

```shell
# Default (ODH CLI image)
mise run e2e:image:build

# Custom CLI image as positional argument
mise run e2e:image:build -- quay.io/my-org/my-cli:v1.0

# Or via env var
OPENSHELL_CLI_IMAGE=quay.io/my-org/my-cli:v1.0 mise run e2e:image:build
```

### Run

The kubeconfig must be readable by UID 1001 inside the container. Copy it to a world-readable location or use `--userns=keep-id`. The results directory must be writable.

```shell
# Prepare kubeconfig and results dir
cp ~/.kube/config /tmp/openshell-e2e-kubeconfig
chmod 644 /tmp/openshell-e2e-kubeconfig
mkdir -m 777 -p ./results

# All suites (e2e-kubernetes + e2e-python)
podman run --rm -t --network host \
  -v /tmp/openshell-e2e-kubeconfig:/home/runner/.kube/config:z,ro \
  -v ./results:/results:z \
  -e OPENSHELL_GATEWAY_IMAGE=ghcr.io/nvidia/openshell/gateway:latest \
  -e OPENSHELL_SUPERVISOR_IMAGE=ghcr.io/nvidia/openshell/supervisor:latest \
  openshell-e2e:dev

# Kubernetes Rust E2E only, smoke test
# Equivalent to: OPENSHELL_E2E_KUBE_TEST=smoke mise run e2e:kubernetes
podman run --rm -t --network host \
  -v /tmp/openshell-e2e-kubeconfig:/home/runner/.kube/config:z,ro \
  -v ./results:/results:z \
  -e OPENSHELL_GATEWAY_IMAGE=ghcr.io/nvidia/openshell/gateway:latest \
  -e OPENSHELL_SUPERVISOR_IMAGE=ghcr.io/nvidia/openshell/supervisor:latest \
  openshell-e2e:dev e2e-kubernetes smoke

# With digest
podman run --rm -t --network host \
  -v /tmp/openshell-e2e-kubeconfig:/home/runner/.kube/config:z,ro \
  -v ./results:/results:z \
  -e OPENSHELL_GATEWAY_IMAGE=quay.io/opendatahub/odh-openshell-gateway@sha256:abc123... \
  -e OPENSHELL_SUPERVISOR_IMAGE=quay.io/opendatahub/odh-openshell-supervisor@sha256:abc123... \
  openshell-e2e:dev e2e-kubernetes

# Python SDK E2E only
podman run --rm -t --network host \
  -v /tmp/openshell-e2e-kubeconfig:/home/runner/.kube/config:z,ro \
  -v ./results:/results:z \
  openshell-e2e:dev e2e-python

# Sidecar topology variant
# Equivalent to: mise run e2e:kubernetes:sidecar
podman run --rm -t --network host \
  -v /tmp/openshell-e2e-kubeconfig:/home/runner/.kube/config:z,ro \
  -v ./results:/results:z \
  -e OPENSHELL_E2E_KUBE_EXTRA_VALUES=/app/deploy/helm/openshell/ci/values-sidecar.yaml \
  openshell-e2e:dev e2e-kubernetes
```

### Available suites

| Suite | Description | JUnit output |
|-------|-------------|--------------|
| `e2e-kubernetes` | Rust E2E tests (pre-compiled nextest archive, `e2e` + `e2e-kubernetes` features) | `rust-e2e-xunit.xml` |
| `e2e-python` | Python SDK integration tests | `python-e2e-xunit.xml` |

The second positional argument is an optional test filter:
- For `e2e-kubernetes`: maps to nextest `--filter "binary(<name>)"` (e.g., `smoke`, `sandbox_lifecycle`, `readyz_health`)
- For `e2e-python`: maps to pytest `-k <pattern>`

### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `OPENSHELL_GATEWAY_IMAGE` | `ghcr.io/nvidia/openshell/gateway` | Gateway image (accepts `repo`, `repo:tag`, or `repo@sha256:...`) |
| `OPENSHELL_SUPERVISOR_IMAGE` | `ghcr.io/nvidia/openshell/supervisor` | Supervisor image (same formats) |
| `OPENSHELL_GATEWAY_TAG` | _(from image ref)_ | Override gateway tag (takes precedence over tag/digest in image ref) |
| `OPENSHELL_SUPERVISOR_TAG` | _(from image ref)_ | Override supervisor tag (same) |
| `OPENSHELL_E2E_KUBE_EXTRA_VALUES` | _(empty)_ | Colon-separated Helm values files (use container paths under `/app/`) |
| `AGENT_SANDBOX_VERSION` | `v0.5.0` | Agent Sandbox CRD version |
| `RESULTS_DIR` | `/results` | JUnit XML output directory |

### Cluster compatibility

The entrypoint auto-detects OpenShift and applies SCC bindings and security context overrides only when needed. On vanilla Kubernetes (kind, k3d, etc.), Helm defaults are used as-is.

## Support Scripts

| Script | Purpose |
|--------|---------|
| `e2e/with-kube-gateway.sh` | Deploy gateway on K8s, run a command, clean up |
| `e2e/with-docker-gateway.sh` | Deploy gateway via Docker |
| `e2e/with-podman-gateway.sh` | Deploy gateway via Podman |
| `e2e/support/gateway-common.sh` | Shared helpers: port picking, PKI generation, gateway registration |
| `e2e/support/install-agent-sandbox.sh` | Install agent-sandbox CRDs and controller |
