# OpenShell and dashboard installer for OpenShift

This ODH-fork installer installs OpenShell and the OpenShell dashboard as two
Helm releases on OpenShift. It is a lifecycle CLI driven from a local working
directory:

| Subcommand | Cluster access | Purpose |
|---|---|---|
| `init` | No | Scaffold `config.json` and both override files |
| `validate` | No | Validate config and render both charts |
| `preflight` | Read-only | Check cluster prerequisites and conflicts |
| `install` | Yes | Install or upgrade the gateway and dashboard |
| `uninstall` | Yes | Remove the releases and installer-owned resources |

The installer installs both components together, always. A gateway-only
deployment is a plain `helm install` of the gateway chart, documented in
[Gateway-only install](#gateway-only-install-advanced) and in
[INSTALL-KEYCLOAK-GUIDE.md](INSTALL-KEYCLOAK-GUIDE.md).

The installer consumes administrator-provided authentication configuration. It
never creates realms, clients, audiences, role mappings, or users in an identity
provider and never handles identity-provider administration credentials.

## Prerequisites

- `uv`, which fetches Python 3.11 or later and PyYAML automatically. No separate
  Python install is needed.
- Helm 3.17.3 or later and `kubectl` on the administrator's workstation.
- An OpenShift cluster and an authenticated session (`oc login ...`). `init` and
  `validate` need neither a cluster nor `kubectl`.
- Agent Sandbox CRDs and a running controller installed by the cluster
  administrator. Override `agent_sandbox_namespace` and
  `agent_sandbox_deployment` if your installation uses different names.
- A CNI enforcing NetworkPolicy and a default block-backed StorageClass for the
  gateway's SQLite PVC.
- Access to `registry.redhat.io` from the cluster nodes for the RHOAI product
  images, with a pull secret in both override files and on the gateway
  (`server.sandboxImagePullSecrets`). The hardened `authProxy` image lives on
  the public `registry.access.redhat.com` and needs no pull secret.
- Permission to manage the namespaced and cluster-scoped resources the charts
  require. Preflight checks the actual operations instead of requiring wildcard
  cluster-admin.

The installer does not install an IdP, Agent Sandbox, RHOAI, a modular operator,
or console integration. It does not change your current kubeconfig context.

## Inputs the installer consumes

An identity-provider administrator supplies these through the organization's
existing process. The installer creates none of them.

| Input | Config field | Scope |
|---|---|---|
| Provider type (`oidc` or `keycloak-oidc`) | `oidc.provider` | shared |
| Issuer URL | `oidc.issuer` | shared |
| CA bundle or existing ConfigMap | `oidc.ca_file` / `oidc.ca_configmap` | shared |
| Claim and role names | `oidc.roles_claim`, `admin_role`, `user_role` | shared |
| OAuth scopes | `oidc.scope` | shared |
| API audience | `oidc.audience` | deployment |
| Browser client ID | `oidc.client_id` | deployment |
| Dashboard OIDC client secret file or existing Secret | `oidc.client_secret_file` / `oidc.client_secret_secret` | deployment |
| Access assignments | `oidc.allowed_groups` / `oidc.allowed_roles` | deployment |
| Dashboard callback URL | derived: `https://<dashboard.host>/oauth2/callback` | deployment |

The Keycloak guide is a companion for administrators who still need to create
those provider registrations. It is not required reading for other providers.

## Quick start

Download the script once into a dedicated working directory, pinned to a tag or
commit SHA, never `main`:

```shell
mkdir openshell-on-rhoai && cd openshell-on-rhoai
curl -LsSfO https://raw.githubusercontent.com/opendatahub-io/openshell/<tag-or-sha>/deploy/rhoai/install.py
chmod +x install.py
```

For RHOAI 3.6, initialize with one command:

```shell
APPS_DOMAIN="$(oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}')"
./install.py init \
  --namespace openshell-team \
  --cluster-app-domain "$APPS_DOMAIN" \
  --rhoai-version 3.6 \
  --oidc-issuer https://identity.example.com/realms/customer
```

`--oidc-issuer` is the OIDC issuer URL: the exact `iss` claim the identity
provider puts in tokens and the base URL for its
`/.well-known/openid-configuration` discovery document. For Keycloak it is
`https://<host>/realms/<realm>`. It is shared by every installation behind that
provider and is not the cluster's apps domain or the dashboard URL. Use the
value verbatim, without a trailing slash.

Write the dashboard OIDC client secret beside the config, then run the remaining
steps. This is the secret of the dashboard's browser client (`oidc.client_id`);
the file name mirrors the in-cluster Secret it becomes,
`openshell-dashboard-oidc`:

```shell
printf '%s\n' '<dashboard OIDC client secret>' > openshell-dashboard-oidc-secret.txt
chmod 600 openshell-dashboard-oidc-secret.txt

./install.py validate
./install.py preflight
./install.py install
```

`init` scaffolds three files in the current directory:

- `config.json`, which wires in both override files through `helm_overrides`.
- `gateway-overrides.yaml` and `dashboard-overrides.yaml`, which hold operational
  Helm values.

To use an administrator-managed Secret instead of a local file, pass
`--oidc-client-secret-secret <name>` to `init`. The Secret must contain
`client-secret` and `cookie-secret`; the installer never creates, labels, or
modifies it.

### RHOAI compatibility

`--rhoai-version` pins the tested chart pair and the product images in one step.
A profile is supported only when both charts and every image resolve at the
recorded versions.

| Profile | Gateway chart | Dashboard chart | Image tag | Pre-release | Images qualified |
|---|---|---|---|---|---|
| `3.6.0` (alias `3.6`) | `0.1.3` | `0.1.0` | `rhoai-3.6` | no | no |

The `3.6.0` image combination has not yet passed the live smoke run against the
frozen 3.6 build, so `init` warns that it is unqualified. Add pre-release and GA
builds of a new line as separate rows with their own chart pins; a bare `X.Y`
resolves only to the GA row and never to an EA. The profile table, this table,
and the exact-command block change together at release time.

The profile writes `registry.redhat.io/rhoai/odh-openshell-*-rhel9` images:

- `gateway`, `supervisor`, and `sandboxRuntime` from
  `odh-openshell-{gateway,supervisor,sandbox}-rhel9`.
- `sandbox` with the full repository path in `gateway.images.sandbox`, because
  the chart's sandbox image has no registry key.
- `dashboard` from `odh-openshell-dashboard-rhel9`.
- `authProxy` from the hardened public image
  `registry.access.redhat.com/hi/oauth2-proxy:7.15.4`. No RHOAI build ships an
  oauth2-proxy image. The chart default is `v7.15.2`; `7.15.4` is a patch bump.
  For mirrored or disconnected installs, set a reviewed mirror.

Without `--rhoai-version` there is no `images` block and the charts' upstream
defaults apply. `init` prints a notice in that case. The explicit
`--openshell-version` and `--openshell-dashboard-version` flags are the advanced,
profile-free path; they are mutually exclusive with `--rhoai-version`.

The chart artifacts themselves remain upstream OCI references; only the images
and the tested chart versions are RHOAI-specific. Set the `registry.redhat.io`
pull secret in both override files and on the gateway:

```yaml
# gateway-overrides.yaml
imagePullSecrets:
  - name: redhat-registry
server:
  sandboxImagePullSecrets:
    - name: redhat-registry
```

```yaml
# dashboard-overrides.yaml
imagePullSecrets:
  - name: redhat-registry
```

The profile pins the `rhoai-3.6` line tag, never a digest. RHOAI rebuilds images
through code freeze and republishes them per z-stream, so digests are recorded in
the validation evidence with the operator CSV version, not in the installer. Pin
a digest per installation with `images.*.digest` when immutability is required.
`preflight` compares the configured images with the cluster's `rhods-operator`
CSV and fails on a repository or digest mismatch. When RHOAI is absent or a tag
cannot be resolved, it warns and continues.

## Configuration

Each `config.json` is self-contained. `load_config` resolves `helm_overrides`,
`oidc.client_secret_file`, and `oidc.ca_file` relative to the config file. The
`gateway` section configures the `openshell` Helm release; the `dashboard`
section configures `openshell-dashboard`.

| Field | Description |
|---|---|
| `namespace` | Target application namespace; system namespaces are rejected |
| `gateway.version` | Exact gateway chart version, not the image tag |
| `dashboard.version` | Exact dashboard chart version, not the image tag |
| `helm_overrides` | Operational YAML values files, merged in order |
| `gateway.host`, `dashboard.host` | Public Route hosts; must differ |
| `oidc.provider` | `oidc` or `keycloak-oidc`, passed to oauth2-proxy |
| `oidc.client_secret_file` | Dashboard OIDC client secret file (the browser client's secret); mutually exclusive with `client_secret_secret` |
| `oidc.client_secret_secret` | Existing administrator-managed Secret with `client-secret` and `cookie-secret`; never owned |
| `oidc.ca_file` | PEM CA bundle; the installer creates and owns `openshell-oidc-ca` |
| `oidc.ca_configmap` | Existing CA ConfigMap; never owned; mutually exclusive with `ca_file` |
| `oidc.allowed_groups` | Dashboard login filter for either provider |
| `oidc.allowed_roles` | Dashboard login filter, valid only with `keycloak-oidc` |
| `rhods_operator_namespace` | Where preflight looks for the RHOAI CSV (default `redhat-ods-operator`) |

There is no `context` field. The installer uses the current kubectl context;
pass `--context` to override it. Every cluster-touching command prints the
resolved context and cluster, and the identity it will use, before its first
cluster call. `preflight`, `install`, and `uninstall` refuse to run when
`current-context` is empty.

`init` derives deployment identity from the namespace unless you override it:

- `client_id` becomes `openshell-dashboard-<namespace>`.
- `audience` becomes `openshell-api-<namespace>`.
- `--cluster-app-domain <domain>` derives `gateway-<namespace>.<domain>` and
  `dashboard-<namespace>.<domain>`.

Derived hosts are always namespace-qualified, so two installations cannot claim
the same host, and the router's default `*.apps.<domain>` certificate covers
them. Use `--gateway-host` and `--dashboard-host` when organizational naming
differs or a long namespace would push a derived label past the 63-character DNS
limit. `validate`, `preflight`, and `install` accept `--namespace`, which changes
the target namespace but never the identity values; the printed identity line
makes a mismatch visible.

`init` is non-interactive and writes only into the current directory. It
refuses to overwrite any of the three files.

### Access filters

`allowed_groups` is valid for either provider. `allowed_roles` requires
`provider: "keycloak-oidc"` and matches the claim named by `roles_claim` (for
Keycloak, realm roles arrive under `realm_access.roles`). The two lists are
independent: selecting `keycloak-oidc` does not remove group configuration, and
both may be present. `allowed_roles` gates dashboard login only; gateway
authorization still uses `admin_role` and `user_role`.

List every role that may sign in. Listing only `openshell-user` locks out
users who have only `openshell-admin`. Keycloak client roles are written
`<client-id>:<role>`. Claim names, role names, filters, and scope are
administrator-supplied values; `init` writes neutral defaults and never infers a
provider layout.

Optional `oidc.scope` supplies space-separated OAuth scopes including `openid`.
Request only scopes your IdP supports; include `groups` if your provider
requires it for group claims.

## Chart values

Set `gateway.helm_overrides` and/or `dashboard.helm_overrides` to lists of YAML
or JSON files relative to the config. Files merge in order: maps merge, later
scalars and lists replace earlier ones. Keep the same files for reruns; the
installer passes the merged result on every upgrade. Start with
[gateway-overrides.example.yaml](gateway-overrides.example.yaml) and
[dashboard-overrides.example.yaml](dashboard-overrides.example.yaml).

| Chart | Values |
|---|---|
| Both | `resources`, `nodeSelector`, `tolerations`, `affinity`, `imagePullSecrets`, `openshiftRoute.annotations` |
| Gateway | `server.workspaceStorageClass`, `server.workspaceDefaultStorageSize`, `server.defaultRuntimeClassName`, `server.sandboxImagePullSecrets` |
| Dashboard | `authProxy.resources`, `authProxy.cookie.refresh`, `authProxy.upstreamTimeout`, `networkPolicy.ingressNamespace`, `networkPolicy.ingressFrom` |

Reference existing image-pull Secrets by name; do not put credentials in values
files. Authentication, TLS, release names, service ports, and namespace wiring
remain owned by the installer. Unsupported overrides fail before cluster access.

For a custom or host-network router, provide reviewed `networkPolicy.ingressFrom`
peers in the dashboard override file instead of the default `openshift-ingress`
namespace identity.

## Validate, preflight, and install

```shell
./install.py validate
./install.py preflight
./install.py install
```

A bare invocation is a usage error, never an install.

`validate` is offline. It validates the config, merges override files, and
renders both charts with `helm pull` and `helm template`. It downloads the
pinned charts but does not check connectivity, SCC admission, token claims, or
runtime behavior. It reads the dashboard OIDC client secret file's existence and
size, not its
contents, and performs no RHOAI CSV lookup.

`preflight` renders both charts and then checks the cluster without mutating it:

- The supported served Sandbox API and the Agent Sandbox controller rollout.
- The default StorageClass and the configured `workspaceStorageClass`.
- Create/patch permissions for installation resources and update/delete
  permissions for Helm release Secrets, including history pruning.
- Route hosts claimed by any other namespace.
- Release and cross-namespace identity conflicts.
- External credential references and their required keys.
- RHOAI product images against the operator CSV.

`install` repeats preflight, creates the namespace when missing, records the
installer identity, creates installer-owned credentials, installs the gateway,
checks its CA Secret, and installs the dashboard with that CA and the internal
gateway Service URL. It waits for Helm readiness and Route admission. Set
`--timeout 15m` for a slower cluster.

For source-chart rendering, use `validate --gateway-chart-directory <path>
--dashboard-chart-directory <path>`. These flags exist only on `validate`, so an
offline render cannot touch a cluster. Source charts carry version `0.0.0`, so
this path renders images with the versions from your config; it is not an
install path.

## Several installations behind one provider

One gateway/dashboard pair runs per namespace. Repeated installations are a
first-class workflow: each has its own working directory, hosts, browser client,
audience, callback URL, and access assignments. Start a second installation in
its own directory and seed only the shared identity settings:

```shell
mkdir -p ../team-b && cd ../team-b
cp ../openshell-on-rhoai/install.py .
./install.py init \
  --from-config ../openshell-on-rhoai/config.json \
  --namespace team-b \
  --cluster-app-domain "$APPS_DOMAIN" \
  --rhoai-version 3.6
```

`--from-config` copies `provider`, `issuer`, `ca_file`/`ca_configmap`,
`roles_claim`, `admin_role`, `user_role`, and `scope`. It never copies the
namespace, hosts, client ID, audience, client secret, or access assignments. A
relative `ca_file` in the source becomes an absolute path. Each generated
config is self-contained; there is no shared runtime file to drift.

Register `https://<dashboard-host>/oauth2/callback` with the IdP for that
namespace's browser client. Provision a distinct API audience and browser client
per gateway, even when gateways share an issuer. `--namespace` does not rewrite
OIDC identifiers or create IdP clients. Preflight rejects an issuer/audience or
issuer/browser-client pair already recorded by another installer-managed
namespace, and rejects a Route host already claimed by another namespace.

## Reruns, upgrades, and failures

Rerun the same command after correcting a failure. The fixed release names
provide one gateway/dashboard pair per namespace. An existing gateway chart under
a different release name, either fixed release without installer state, or
unmanaged gateway TLS/JWT Secrets requires explicit migration. Unrelated Helm
releases are left alone. Preflight inspects stored Helm workloads for the
`openshell-gateway` container or executable, so renaming the chart or overriding
its image does not bypass gateway detection.

The `openshell-installer` ConfigMap records the gateway host, issuer, audience,
browser client ID, and credential ownership. Changing the recorded identity or
hostname requires explicit migration. Legacy state is populated on the next
successful install without rewriting existing values; concurrent updates use a
resource-version precondition.

The installer preserves existing client and cookie secrets. A first installation
requires a client secret file or an existing Secret. A provided file that differs
from the existing Secret causes an error; rotate credentials explicitly and
restart the consuming pods. Removing `ca_file` removes the mounts on the next
upgrade but does not delete the old ConfigMap.

Change the pinned versions or the profile to upgrade. Both releases receive the
generated values with `--reset-values`, so out-of-band Helm edits are not
preserved. There is no cross-release transaction: a successful gateway upgrade
followed by a failed dashboard upgrade leaves the upgraded gateway in place.
Failed releases, namespaces, Secrets, PVCs, and installer state are preserved for
diagnosis.

## Uninstall

```shell
./install.py uninstall
```

`uninstall` removes the Helm releases and the resources the installer owns. It
never deletes the namespace.

Before anything destructive it proves ownership: the `openshell-installer`
ConfigMap carries `openshell.ai/installer=odh-dashboard`, or the namespace carries
that label, or it has the legacy `openshell.ai/installer-gateway-host`
annotation. A namespace without that proof is refused by name, which makes
`uninstall --namespace` safe.

It discovers installed releases from `helm list` rather than trusting the
config, then prints the full inventory. On a terminal it asks you to type the
namespace name; when stdin is not a terminal it requires `--yes`.

Teardown is ordered and resumable:

1. The dashboard release first, because it depends on the gateway's TLS material
   and the OIDC Secret.
2. The gateway release.
3. The three hook-created TLS Secrets (`openshell-server-tls`,
   `openshell-client-tls`, `openshell-jwt-keys`). The chart's certgen hook
   creates them at runtime, outside the release manifest and without Helm
   labels, so `helm uninstall` never removes them. Their names are
   installer-fixed, and deletion rides on the ownership proof above.
4. Installer-owned credentials only. `openshell-dashboard-oidc` and
   `openshell-oidc-ca` are deleted when the installer created them. A Secret
   named by `oidc.client_secret_secret` or a ConfigMap named by
   `oidc.ca_configmap` is administrator-managed and is preserved, even when it
   reuses a fixed installer name.
5. `openshell-installer`, only after every earlier step succeeded.

Dependent cleanup respects failures. If the dashboard release cannot be removed,
the gateway, TLS Secrets, credentials, and state are retained because the
surviving dashboard still needs them. If the gateway cannot be removed, the TLS
Secrets, credentials, and state are retained. A failed secret or ConfigMap
deletion preserves the state so a rerun passes the ownership gate and finishes
the job. The command names every retained resource and exits non-zero. Rerun it
after fixing the cause.

Cluster-scoped RBAC belongs to the Helm releases. The installer never deletes it
by label, because the label is namespace-agnostic and would remove another
installation's RBAC. It reports survivors by name.

The namespace itself is kept, so anything else deployed there is untouched. To
remove it too, do so explicitly as a separate, optional step:

```shell
oc delete namespace openshell-team
```

## Gateway-only install (advanced)

The installer deliberately installs both components. For gateway-only, run a
plain Helm install of the gateway chart: the coupling runs one way — the
dashboard needs the gateway's CA and internal Service URL, while the gateway
knows nothing about the dashboard. A manual gateway-only install also drops the
dashboard's external OCI chart dependency and needs no browser OIDC client. Only
the API audience client and the CLI client from the Keycloak guide are required.

Cover the prerequisites preflight would otherwise check: Agent Sandbox CRDs and
a running controller, a default block-backed StorageClass, an application
namespace, and permission to create the cluster-scoped RBAC the chart needs.

Create `gateway-only.yaml` mirroring the gateway half of the installer's
rendered values. `install.py`'s `chart_values` is the reference implementation:

```yaml
fullnameOverride: openshell
podSecurityContext: null
securityContext:
  runAsUser: null
  seccompProfile:
    type: RuntimeDefault
replicaCount: 1
server:
  disableTls: false
  sandboxNamespace: openshell-team
  auth:
    allowUnauthenticatedUsers: false
  tls:
    enableMtls: false
    clientCaSecretName: ""
    certSecretName: openshell-server-tls
    clientTlsSecretName: openshell-client-tls
  oidc:
    issuer: https://identity.example.com/realms/customer
    audience: openshell-api-openshell-team
    rolesClaim: roles
    adminRole: openshell-admin
    userRole: openshell-user
    # caConfigMapName: openshell-oidc-ca  # only for a private CA
pkiInitJob:
  enabled: true
  serverDnsNames:
    - gateway-openshell-team.apps.example.com
    - openshell.openshell-team.svc
    - openshell.openshell-team.svc.cluster.local
openshiftRoute:
  enabled: true
  host: gateway-openshell-team.apps.example.com
```

If the issuer uses a private CA, create the ConfigMap yourself; the gateway
chart does not create it:

```shell
oc -n openshell-team create configmap openshell-oidc-ca --from-file=ca.crt=issuer.pem
```

Install with an exact chart version:

```shell
helm upgrade --install openshell oci://ghcr.io/nvidia/openshell/helm-chart \
  --version 0.1.3 \
  --namespace openshell-team \
  --values gateway-only.yaml \
  --wait --wait-for-jobs --timeout 10m
```

This path gives up preflight checks, the cross-namespace identity reservation,
Route-admission waiting, and `uninstall`. Teardown is `helm uninstall openshell`
plus removing the three hook-created TLS Secrets, which Helm orphans:

```shell
helm uninstall openshell -n openshell-team
oc -n openshell-team delete secret openshell-server-tls openshell-client-tls openshell-jwt-keys
```

Registering the gateway with the CLI works identically after a manual install.

## TLS and onboarding

The gateway uses the chart's PKI bootstrap hook and a passthrough Route. Generated
server SANs include the gateway public host and namespace-specific Service names.
This OIDC-only mode uses a private gateway CA: export and distribute its `ca.crt`
from `openshell-client-tls` through a trusted administrator process. Register the
gateway with the installed CLI's CA options from `openshell gateway add --help`;
do not disable certificate verification. TLS client-certificate authentication
is disabled; supervisors retain gateway-minted sandbox JWT authentication.

The dashboard uses an edge Route and trusts the gateway CA for internal calls.
Its authentication proxy is the only exposed dashboard port. After installation,
verify administrator and standard-user login, denied cross-workspace access,
sandbox creation and deletion, and terminal WebSockets before sharing the
deployment. Readiness and Route admission alone do not prove that flow works.

## Authorization verification

The read-only verifier stays a separate script, run from the checkout, because it
probes authorization with two bearer tokens and two existing workspaces and
shares none of the installer config. Save fresh access tokens in files with mode
`0600` outside the checkout:

```shell
uv run --script --python 3.12 deploy/rhoai/verify.py \
  --context YOUR_CONTEXT --namespace openshell-team \
  --dashboard-url https://dashboard-openshell-team.apps.example.com \
  --admin-role openshell-admin \
  --admin-token-file /secure/admin-token.txt \
  --user-token-file /secure/user-token.txt \
  --allowed-workspace team-a --denied-workspace team-b
```

The verifier checks public unauthenticated rejection, gateway identity and roles,
admin access to both workspaces, ordinary-user access to one and denial in the
other. It opens a temporary loopback-only port-forward to the BFF, which requires
`pods/portforward` permission. It performs GET requests only. It never follows
redirects with credentials or prints tokens or response bodies.

Optional `--other-gateway-token-file` checks rejection of a token for another
gateway; first confirm that token works on its intended gateway. Optional
`--session-cookie-file` checks the public session endpoint with a browser Cookie
header. `--ca-file` adds a private Route CA to system trust. The verifier does
not automate browser login, grant/revoke, sandbox lifecycle, terminal execution,
or network-policy enforcement.

## Live validation

On October 13, 2026, the installer was exercised on an OpenShift `4.21.8` cluster
with RHOAI installed, OVN Kubernetes, `gp3-csi` storage, Agent Sandbox `v1.0.6`,
and an existing Red Hat build of Keycloak `26.4.10`. A dedicated test realm
supplied OIDC; existing customer realms were not changed.

The following checks passed with gateway chart `0.1.3` and dashboard chart
`0.1.0` using the charts' default upstream images:

- First installation into a new namespace and a rerun with the same inputs.
- Installation and rerun in a pre-created namespace with both releases reaching
  revision 2 and the namespace's labels, annotations, and SCC allocations
  preserved.
- Gateway readiness, both dashboard containers ready, and persistent storage
  bound.
- Public HTTPS routing and HTTP 401 for unauthenticated dashboard API requests.
- Browser OIDC redirect/login/callback and authenticated workspace and gateway
  API calls.
- Sandbox creation through the dashboard API, supervisor readiness, authenticated
  terminal WebSocket execution with exit code 0, and completed deletion.
- Read-only authorization verification, including denied access after membership
  revocation.

These checks do not establish the RHOAI product-image combination, FIPS
qualification, production certificate renewal, or a full UI interaction test.
The `3.6.0` profile remains marked unqualified until the frozen 3.6 build passes
the same checks with the `registry.redhat.io` images. The live configuration and
credentials are kept outside the repository. Record the OpenShift version, RHOAI
version and operator CSV, chart versions, product image digests from that CSV,
Agent Sandbox version, and the `authProxy` digest when a build is qualified.

## Contributor validation

The `RHOAI Installer` GitHub Actions workflow runs both suites for changes to
this directory. It needs no cluster credentials or IdP. Run them locally with:

```shell
uv run --script --python 3.12 deploy/rhoai/test_install.py
uv run --no-project --python 3.12 deploy/rhoai/test_verify.py
uvx ruff==0.14.14 check deploy/rhoai
uvx ruff==0.14.14 format --check deploy/rhoai
```

`mise.toml` deliberately excludes `deploy/rhoai` from its Python paths.
