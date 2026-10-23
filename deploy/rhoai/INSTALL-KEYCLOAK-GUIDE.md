# Install Keycloak and OpenShell on OpenShift (ROSA)

This guide has two audiences. Part 2 is identity-provider administration: the
one-time work a Keycloak administrator performs to create the realm, clients,
audiences, roles, groups, and callback URLs OpenShell consumes. The installer
never performs that work. Parts 1 and 3 describe a complete reference
deployment; other providers skip Part 1 and supply the same inputs another way.

The installer always installs the gateway and dashboard together. If you want
only the gateway, see [Gateway-only install](#gateway-only-install-advanced).

## Prerequisites

- `oc` CLI authenticated to the cluster
- `uv`, Helm 3.17.3+, and `kubectl` on your workstation
- Agent Sandbox CRDs and a running controller on the cluster
- Access to `registry.redhat.io`, `registry.access.redhat.com`, and `quay.io`
  from the cluster nodes, with a pull secret for `registry.redhat.io`

## Set your cluster's apps domain

All commands below use `APPS_DOMAIN`. Extract it from the cluster:

```shell
export APPS_DOMAIN="$(oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}')"
export NS=openshell-team
export API_ID="openshell-api-${NS}"
export DASH_ID="openshell-dashboard-${NS}"
export DASH_URL="https://dashboard-${NS}.${APPS_DOMAIN}"
export GW_URL="https://gateway-${NS}.${APPS_DOMAIN}"
echo "$APPS_DOMAIN"
```

The namespace-qualified client IDs, audience, hosts, and callback URL above are
the conventions `install.py init --namespace "$NS"` derives. Change `NS` to use
another namespace; the rest follows.

## Part 1 — Install PostgreSQL and Keycloak

### 1.1 Create the namespace

```shell
oc new-project keycloak
```

### 1.2 Create the PostgreSQL database secret

```shell
oc -n keycloak create secret generic keycloak-db-secret \
  --from-literal=username=keycloak \
  --from-literal=password="$(openssl rand -base64 24)"
```

### 1.3 Deploy PostgreSQL with persistent storage

> **Note:** This is a development setup. Use a managed database or a production
> PostgreSQL operator for production.

```shell
# Create the PVC
oc -n keycloak apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: keycloak-db-data
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 5Gi
EOF

# Deploy PostgreSQL using the Red Hat Hardened Image.
# PGDATA points to a subdirectory to avoid the lost+found conflict
# on the PVC mount point.
oc -n keycloak new-app registry.access.redhat.com/hi/postgresql:18.4 \
  -e POSTGRES_USER=keycloak \
  -e POSTGRES_PASSWORD="$(oc -n keycloak get secret keycloak-db-secret -o jsonpath='{.data.password}' | base64 -d)" \
  -e POSTGRES_DATABASE=keycloak \
  -e PGDATA=/var/lib/postgresql/data/pgdata \
  --name=keycloak-db

# Replace the image's emptyDir volume with the PVC.
# The image creates a volume named "keycloak-db-volume-1" at /var/lib/postgresql.
oc -n keycloak set volume deployment/keycloak-db \
  --add --overwrite --name=keycloak-db-volume-1 \
  --type=persistentVolumeClaim \
  --claim-name=keycloak-db-data \
  --mount-path=/var/lib/postgresql

# Wait for rollout
oc -n keycloak rollout status deployment/keycloak-db
```

Verify:

```shell
oc -n keycloak get pvc keycloak-db-data
oc -n keycloak get pods -l app=keycloak-db
```

### 1.4 Install the Red Hat build of Keycloak Operator

1. Open the OpenShift console → **Operators** → **OperatorHub**
2. Search for **"Red Hat build of Keycloak"**
3. Install it (namespace: `keycloak` or all namespaces)
4. Wait until the operator is ready:

```shell
oc -n keycloak get pods -l app.kubernetes.io/name=keycloak-operator
```

### 1.5 Create the Keycloak instance

```shell
oc -n keycloak apply -f - <<EOF
apiVersion: k8s.keycloak.org/v2beta1
kind: Keycloak
metadata:
  name: keycloak
  namespace: keycloak
spec:
  instances: 1
  db:
    vendor: postgres
    host: keycloak-db
    usernameSecret:
      name: keycloak-db-secret
      key: username
    passwordSecret:
      name: keycloak-db-secret
      key: password
  http:
    httpEnabled: true
  hostname:
    hostname: keycloak.${APPS_DOMAIN}
  proxy:
    headers: xforwarded
EOF
```

Wait for Keycloak to be ready:

```shell
oc -n keycloak get keycloak keycloak -w
```

### 1.6 Expose Keycloak and get admin credentials

Because the Keycloak CR sets `spec.hostname.hostname`, the operator
automatically creates an edge-terminated Route (with HTTP→HTTPS redirect) for
that hostname. Do not create one manually; doing so causes a hostname collision.

```shell
# Confirm the operator-managed Route exists and owns the hostname
oc -n keycloak get route
```

You should see an operator-managed route named `keycloak-ingress-<suffix>` with
`edge/Redirect` termination serving `keycloak.${APPS_DOMAIN}`:

```
NAME                     HOST/PORT                              SERVICES           PORT   TERMINATION
keycloak-ingress-xxxxx   keycloak.<apps-domain>                 keycloak-service   http   edge/Redirect
```

```shell
# Print the admin console URL and the bootstrap credentials.
# Note: the operator names the bootstrap admin "temp-admin", not "admin".
echo "console:  https://keycloak.${APPS_DOMAIN}/admin/"
echo "username: $(oc -n keycloak get secret keycloak-initial-admin -o jsonpath='{.data.username}' | base64 -d)"
echo "password: $(oc -n keycloak get secret keycloak-initial-admin -o jsonpath='{.data.password}' | base64 -d)"
```

Log in to the admin console URL printed above (the **master** realm) with the
username and password from above. The bootstrap user is `temp-admin`. Create
your own permanent admin user afterwards, as the bootstrap credentials are
temporary.

## Part 2 — Configure the Keycloak realm for OpenShell

This part is the identity-provider administration work. If a Keycloak
administrator already created the registrations below, skip to Part 3 and use
the values they provide in `config.json`. All steps are performed in the
Keycloak admin console unless noted.

### 2.1 Create a realm

A realm is an isolated tenant in Keycloak that manages its own users, clients,
roles, and authentication flows. Each realm has separate token endpoints and
login pages.

- Realm dropdown → **Create realm** → Name: `openshell`

### 2.2 Create realm roles

- **Realm roles** → **Create role** → `openshell-admin`
- **Realm roles** → **Create role** → `openshell-user`

### 2.3 Create users

- **Users** → **Create user** → set username and email
  - **Credentials** tab: set a password
  - **Role mappings** tab: assign `openshell-admin`
- Repeat for a regular user, assign `openshell-user`

### 2.4 Create the API audience client (resource server)

- **Clients** → **Create client**
- **Client ID**: `openshell-api-${NS}`
- **Client authentication**: Off
- **Always display in UI**: Off (default)
- No redirect URIs needed

> **Note:** Leave **Always display in UI** Off for both clients. It only controls
> whether the client appears in the end-user Account Console "Applications" list
> and has no effect on the OIDC token flows OpenShell uses.

### 2.5 Create the dashboard browser client

- **Clients** → **Create client**
- **Client ID**: `openshell-dashboard-${NS}`
- **Client authentication**: On (confidential)
- **Always display in UI**: Off (default)
- **Authentication flow**: ✅ Standard flow, optionally ✅ Direct access grants

Print the exact values to paste into the console form:

```shell
echo "Root URL:                        ${DASH_URL}"
echo "Home URL:                        ${DASH_URL}"
echo "Valid redirect URIs:             ${DASH_URL}/oauth2/callback"
echo "Valid post logout redirect URIs: ${DASH_URL}/*"
echo "Web origins:                     ${DASH_URL}"
```

Under **Settings**:

- **Root URL**: `${DASH_URL}` (optional, recommended)
- **Home URL**: `${DASH_URL}` (optional)
- **Valid redirect URIs**: `${DASH_URL}/oauth2/callback`
- **Valid post logout redirect URIs**: `${DASH_URL}/*`
- **Web origins**: `${DASH_URL}`

Under **Advanced**:

- **Proof Key for Code Exchange**: `S256`

Under **Client scopes** → **openshell-dashboard-${NS}-dedicated** → **Add mapper**
→ **Audience**:

- Name: `openshell-api-audience`
- Included Client Audience: `openshell-api-${NS}`
- Add to access token: ✅ On

Under **Credentials** tab:

- Copy the **client secret** — you will need it in Part 3.

### 2.6 Alternative: create both clients from the command line

Sections 2.4 and 2.5 create the two clients in the admin console. If you prefer,
create them both through the Keycloak admin REST API instead. This produces the
same result and writes the dashboard OIDC client secret to the file Part 3.2 expects,
so you can skip 2.4, 2.5, and Part 3.2. You still need the realm (2.1), roles
(2.2), and users (2.3).

Run it after the `openshell` realm exists. It is idempotent: rerunning skips
clients and mappers that already exist. It requires `oc`, `curl`, and `python3`.

```shell
KC="https://keycloak.${APPS_DOMAIN}"
REALM="openshell"

# Admin token from the operator's bootstrap credentials (master realm).
AU=$(oc -n keycloak get secret keycloak-initial-admin -o jsonpath='{.data.username}' | base64 -d)
AP=$(oc -n keycloak get secret keycloak-initial-admin -o jsonpath='{.data.password}' | base64 -d)
TOK=$(curl -sk -d client_id=admin-cli -d "username=$AU" --data-urlencode "password=$AP" \
  -d grant_type=password "$KC/realms/master/protocol/openid-connect/token" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
H="Authorization: Bearer $TOK"
API="$KC/admin/realms/$REALM/clients"
exists() { curl -sk -H "$H" "$API?clientId=$1" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d[0]['id'] if d else '')"; }

# API audience client (public, no login flow).
if [ -z "$(exists "$API_ID")" ]; then
  curl -sk -X POST -H "$H" -H "Content-Type: application/json" "$API" -d "{
    \"clientId\":\"$API_ID\",\"enabled\":true,\"protocol\":\"openid-connect\",
    \"publicClient\":true,\"standardFlowEnabled\":false,\"directAccessGrantsEnabled\":false
  }"
fi

# Dashboard browser client (confidential).
if [ -z "$(exists "$DASH_ID")" ]; then
  curl -sk -X POST -H "$H" -H "Content-Type: application/json" "$API" -d "{
    \"clientId\":\"$DASH_ID\",\"enabled\":true,\"protocol\":\"openid-connect\",
    \"publicClient\":false,\"standardFlowEnabled\":true,\"directAccessGrantsEnabled\":false,
    \"redirectUris\":[\"$DASH_URL/oauth2/callback\"],\"webOrigins\":[\"$DASH_URL\"],
    \"attributes\":{\"post.logout.redirect.uris\":\"$DASH_URL/*\",\"pkce.code.challenge.method\":\"S256\"}
  }"
fi
ID=$(exists "$DASH_ID")

# Audience mapper: include the API audience in the dashboard's access tokens.
if [ "$(curl -sk -H "$H" "$API/$ID/protocol-mappers/models" \
    | python3 -c "import sys,json;print(any(m['name']=='openshell-api-audience' for m in json.load(sys.stdin)))")" != "True" ]; then
  curl -sk -X POST -H "$H" -H "Content-Type: application/json" "$API/$ID/protocol-mappers/models" -d "{
    \"name\":\"openshell-api-audience\",\"protocol\":\"openid-connect\",\"protocolMapper\":\"oidc-audience-mapper\",
    \"config\":{\"included.client.audience\":\"$API_ID\",\"id.token.claim\":\"false\",\"access.token.claim\":\"true\"}
  }"
fi

# Save the dashboard OIDC client secret where Part 3.2 expects it. Run this from
# the installer working directory created in Part 3.1 (or move the file there
# before installing).
curl -sk -H "$H" "$API/$ID/client-secret" \
  | python3 -c "import sys,json;sys.stdout.write(json.load(sys.stdin)['value'])" \
  > openshell-dashboard-oidc-secret.txt
chmod 600 openshell-dashboard-oidc-secret.txt
echo "Clients ready; dashboard OIDC client secret written to openshell-dashboard-oidc-secret.txt"
```

> **Already ran the installer before creating the client?** The installer copies
> the secret file into the in-cluster `openshell-dashboard-oidc` Secret at
> install time. After the script above, resync that Secret and restart the
> dashboard:
>
> ```shell
> COOKIE=$(oc -n "$NS" get secret openshell-dashboard-oidc -o jsonpath='{.data.cookie-secret}' | base64 -d)
> oc -n "$NS" create secret generic openshell-dashboard-oidc \
>   --from-literal=client-secret="$(cat openshell-dashboard-oidc-secret.txt)" \
>   --from-literal=cookie-secret="$COOKIE" \
>   --dry-run=client -o yaml | oc apply -f -
> oc -n "$NS" rollout restart deployment openshell-dashboard
> ```

If you supplied an administrator-managed Secret with
`--oidc-client-secret-secret`, update that Secret instead; the installer never
modifies it.

### 2.7 Create the CLI client (for `openshell gateway add`)

The `openshell` CLI logs in to the gateway with the Authorization Code + PKCE
flow using a public client whose ID defaults to `openshell-cli`. In the admin
console:

- **Clients → Create client**
- **Client ID**: `openshell-cli`
- **Client authentication**: Off (public)
- **Authentication flow**: ✅ Standard flow. Also enable **OAuth 2.0 Device
  Authorization Grant** if you want headless login (`OPENSHELL_NO_BROWSER=1`).
- **Valid redirect URIs**: `http://127.0.0.1/*` (the CLI binds `127.0.0.1` on a
  random port with path `/callback`, so a wildcard port is required). Optionally
  also `http://localhost/*`.
- Under **Advanced → Proof Key for Code Exchange**: `S256`
- Under **Client scopes → openshell-cli-dedicated → Add mapper → Audience**:
  - Name: `openshell-api-audience`
  - Included Client Audience: `openshell-api-${NS}`
  - Add to access token: ✅ On

The audience mapper is required: the gateway validates `aud == openshell-api-${NS}`,
and Keycloak only emits that claim through a mapper, not from the CLI's
`audience` request parameter.

Or create it from the command line (reusing `$H` / `$API` from 2.6):

```shell
curl -sk -X POST -H "$H" -H "Content-Type: application/json" "$API" -d "{
  \"clientId\":\"openshell-cli\",\"enabled\":true,\"protocol\":\"openid-connect\",
  \"publicClient\":true,\"standardFlowEnabled\":true,\"directAccessGrantsEnabled\":false,
  \"redirectUris\":[\"http://127.0.0.1/*\",\"http://localhost/*\"],
  \"attributes\":{\"pkce.code.challenge.method\":\"S256\",\"oauth2.device.authorization.grant.enabled\":\"true\"}
}"
ID=$(curl -sk -H "$H" "$API?clientId=openshell-cli" | python3 -c "import sys,json;print(json.load(sys.stdin)[0]['id'])")
curl -sk -X POST -H "$H" -H "Content-Type: application/json" "$API/$ID/protocol-mappers/models" -d "{
  \"name\":\"openshell-api-audience\",\"protocol\":\"openid-connect\",\"protocolMapper\":\"oidc-audience-mapper\",
  \"config\":{\"included.client.audience\":\"$API_ID\",\"id.token.claim\":\"false\",\"access.token.claim\":\"true\"}
}"
```

## Part 3 — Install OpenShell

### 3.1 Set up the installer working directory

The installer is a single script. Download it once into a dedicated working
directory — pinned to a tag or commit SHA, never `main` — and run every
subcommand from there, beside the config and override files it scaffolds:

```shell
mkdir openshell-on-rhoai && cd openshell-on-rhoai
curl -LsSfO https://raw.githubusercontent.com/opendatahub-io/openshell/<tag-or-sha>/deploy/rhoai/install.py
chmod +x install.py

./install.py init \
  --namespace "$NS" \
  --cluster-app-domain "$APPS_DOMAIN" \
  --rhoai-version 3.6 \
  --oidc-issuer "https://keycloak.${APPS_DOMAIN}/realms/openshell"
```

`init` creates `config.json`, `gateway-overrides.yaml`, and
`dashboard-overrides.yaml` with owner-only permissions, and refuses to overwrite
files that already exist. It needs neither `kubectl` nor a cluster connection.

The flags fill the shared and deployment values together:

- `--namespace` sets the namespace and derives
  `client_id: openshell-dashboard-${NS}` and `audience: openshell-api-${NS}`.
- `--cluster-app-domain` derives `gateway-${NS}.${APPS_DOMAIN}` and
  `dashboard-${NS}.${APPS_DOMAIN}`.
- `--rhoai-version 3.6` pins gateway chart `0.1.3`, dashboard chart `0.1.0`, and
  the `registry.redhat.io/rhoai/odh-openshell-*-rhel9:rhoai-3.6` images.
- `--oidc-issuer` sets the shared issuer.

Without `--rhoai-version`, the config deploys the charts' upstream images and
`init` prints a notice. `init` warns while the 3.6 image combination remains
unqualified.

Add the Keycloak-specific values to `config.json`. `init` writes neutral values;
it never infers a provider preset:

```json
"oidc": {
  "provider": "keycloak-oidc",
  "roles_claim": "realm_access.roles",
  "allowed_roles": ["openshell-admin", "openshell-user"],
  "scope": "openid email profile"
}
```

Keep `admin_role` and `user_role` at your realm's role names. `allowed_groups`
is still valid with `keycloak-oidc`; the two filters are independent.

### 3.2 Save the dashboard OIDC client secret

Write the dashboard OIDC client secret (the browser client's secret) from 2.5
into the same directory:

```shell
echo -n "PASTE_CLIENT_SECRET_HERE" > openshell-dashboard-oidc-secret.txt
chmod 600 openshell-dashboard-oidc-secret.txt
```

Alternatively, delete that file reference and pass
`--oidc-client-secret-secret <name>` to `init` to reuse an administrator-managed
Secret containing `client-secret` and `cookie-secret`.

### 3.3 Review config.json

`init` fills every value except the secret. The final shape, with
`<apps-domain>` standing in for your domain, is:

```json
{
  "namespace": "openshell-team",
  "gateway": {
    "chart": "oci://ghcr.io/nvidia/openshell/helm-chart",
    "version": "0.1.3",
    "helm_overrides": ["gateway-overrides.yaml"],
    "host": "gateway-openshell-team.<apps-domain>",
    "images": {
      "gateway": {
        "registry": "registry.redhat.io",
        "repository": "rhoai/odh-openshell-gateway-rhel9",
        "tag": "rhoai-3.6"
      },
      "supervisor": {
        "registry": "registry.redhat.io",
        "repository": "rhoai/odh-openshell-supervisor-rhel9",
        "tag": "rhoai-3.6"
      },
      "sandboxRuntime": {
        "registry": "registry.redhat.io",
        "repository": "rhoai/odh-openshell-sandbox-rhel9",
        "tag": "rhoai-3.6"
      },
      "sandbox": {
        "repository": "registry.redhat.io/rhoai/odh-openshell-sandbox-rhel9",
        "tag": "rhoai-3.6"
      }
    }
  },
  "dashboard": {
    "chart": "oci://ghcr.io/gkrumbach07/openshell-dashboard/helm-chart/openshell-dashboard",
    "version": "0.1.0",
    "helm_overrides": ["dashboard-overrides.yaml"],
    "host": "dashboard-openshell-team.<apps-domain>",
    "images": {
      "dashboard": {
        "registry": "registry.redhat.io",
        "repository": "rhoai/odh-openshell-dashboard-rhel9",
        "tag": "rhoai-3.6"
      },
      "authProxy": {
        "registry": "registry.access.redhat.com",
        "repository": "hi/oauth2-proxy",
        "tag": "7.15.4"
      }
    }
  },
  "oidc": {
    "provider": "keycloak-oidc",
    "issuer": "https://keycloak.<apps-domain>/realms/openshell",
    "client_id": "openshell-dashboard-openshell-team",
    "client_secret_file": "openshell-dashboard-oidc-secret.txt",
    "audience": "openshell-api-openshell-team",
    "roles_claim": "realm_access.roles",
    "admin_role": "openshell-admin",
    "user_role": "openshell-user",
    "allowed_roles": ["openshell-admin", "openshell-user"],
    "scope": "openid email profile"
  }
}
```

There is no `context` field: the installer uses your current kubectl context
(the one `oc login` set). `preflight`, `install`, and `uninstall` print the
resolved context, cluster, and OIDC identity before touching anything and refuse
to run when no context is set; pass `--context` to override. `verify.py` (not
used in this guide) takes a required `--context`.

If you edited a `${APPS_DOMAIN}` placeholder by hand instead of using the flags,
expand it now. Only `APPS_DOMAIN` is substituted so other `$` values are left
untouched:

```shell
APPS_DOMAIN="$APPS_DOMAIN" envsubst '${APPS_DOMAIN}' < config.json > config.json.tmp \
  && mv config.json.tmp config.json
grep -n '${APPS_DOMAIN}' config.json || echo "OK: all placeholders expanded"
```

Key fields explained:

| Field | Description |
|---|---|
| `gateway.version` | Helm chart version for the gateway (not the image tag) |
| `dashboard.version` | Helm chart version for the dashboard (not the image tag) |
| `helm_overrides` | Operational values files, merged in order (see the README's "Chart values"); `init` wires in the two generated files |
| `gateway.host`, `dashboard.host` | Public DNS for each Route; namespace-qualified by default |
| `images` | Product images written by `--rhoai-version`; omit the flag for upstream defaults |
| `sandbox.repository` | Full registry/repository path (the chart has no separate registry key for this image) |
| `provider` | `keycloak-oidc` enables Keycloak-specific oauth2-proxy behavior |
| `roles_claim` | `realm_access.roles` is where Keycloak puts realm roles in the JWT |
| `allowed_roles` | Dashboard login gate. Include **both** `openshell-admin` and `openshell-user`, or admin-only users are blocked at login. Plain realm-role names apply because `roles_claim` is `realm_access.roles`; client roles are written `<client-id>:<role>`. |
| `authProxy` | Hardened public image; no RHOAI build ships an oauth2-proxy image |

Add a `registry.redhat.io` pull secret to both override files and to
`server.sandboxImagePullSecrets` on the gateway:

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

### 3.4 Validate, preflight, and install

```shell
# Dry-run validation (no cluster access)
./install.py validate

# Cluster preflight (reads cluster state, does not install)
./install.py preflight

# Install
./install.py install
```

`validate` and `preflight` are separate steps on purpose: a bare `./install.py`
is a usage error, never an install. Every cluster-facing subcommand prints the
resolved kubectl context and cluster first and refuses to run when no context is
set. `preflight` also compares the configured product images with the
`rhods-operator` CSV and warns when RHOAI is absent or a tag cannot be resolved.

## Part 4 — Register the gateway and run a sandbox

Run these from your workstation with the `openshell` CLI. They assume the
`openshell-cli` client exists (2.7).

> **ℹ️ Prerequisite**
>
> Install the OpenShell CLI version for this gateway before you add the provider.
>
> ```shell
> curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | OPENSHELL_VERSION=v0.1.3 sh
> ```

### 4.1 Trust the gateway's server CA

The gateway Route is TLS passthrough serving a self-signed certificate, so the
CLI must trust the gateway CA. Copy it from the `openshell-client-tls` Secret
into the CLI's per-gateway TLS directory — the directory name must match the
`--name` you use in 4.2:

```shell
mkdir -p ~/.config/openshell/gateways/openshell-on-openshift-gw/mtls
oc -n "$NS" get secret openshell-client-tls \
  -o jsonpath='{.data.ca\.crt}' | base64 -d \
  > ~/.config/openshell/gateways/openshell-on-openshift-gw/mtls/ca.crt
```

For a quick test you can instead pass `--gateway-insecure` (or set
`OPENSHELL_GATEWAY_INSECURE=1`) to skip verification — not recommended beyond
local testing.

### 4.2 Register the gateway

```shell
openshell gateway add \
  --name openshell-on-openshift-gw \
  --oidc-issuer "https://keycloak.${APPS_DOMAIN}/realms/openshell" \
  --oidc-client-id openshell-cli \
  --oidc-audience "$API_ID" \
  "${GW_URL}:443"
```

This opens a browser for the Authorization Code + PKCE login. `--oidc-client-id`
defaults to `openshell-cli`, so you may omit it. The port is `443` (the
passthrough Route), which forwards to the gateway's gRPC port.

### 4.3 Add an inference provider and select a model

Example using Claude on Google Vertex AI with Application Default Credentials:

```shell
openshell provider create \
  --name my-gcp \
  --type google-vertex-ai \
  --from-gcloud-adc \
  --config VERTEX_AI_PROJECT_ID="$VERTEX_AI_PROJECT_ID" \
  --config VERTEX_AI_REGION=global

openshell inference set --provider my-gcp --model claude-haiku-4-5
```

### 4.4 Create and connect to a sandbox

```shell
DRIVER_CONFIG='{"kubernetes":{"containers":{"agent":{"resources":{"requests":{"cpu":"100m","memory":"512Mi"},"limits":{"cpu":"500m","memory":"512Mi"}}}}}}'

openshell sandbox create \
  --name mysand \
  --driver-config-json "$DRIVER_CONFIG" \
  --env=ANTHROPIC_BASE_URL=https://inference.local \
  --env=ANTHROPIC_API_KEY=unused \
  --no-auto-providers \
  -- claude --bare --model claude-haiku-4-5

openshell sandbox connect mysand --editor vscode
```

Disconnecting does not stop the sandbox process; `connect` reattaches to the
same process and replays recent output. Supported interactive editors are
`vscode` and `cursor`.

## Gateway-only install (advanced)

The installer deliberately installs the gateway and dashboard together. If you
want only the gateway, that is a plain `helm install` of the gateway chart: the
coupling runs one way — the dashboard needs the gateway's CA and internal
Service URL, while the gateway knows nothing about the dashboard. A manual
gateway-only install also drops the dashboard's external OCI chart dependency
and needs no browser OIDC client. From Part 2, only the API audience client
(2.4) and the CLI client (2.7) are required; skip 2.5 and 2.6.

Before installing manually, cover the prerequisites the installer's preflight
would otherwise check:

- The Agent Sandbox CRDs and a running controller.
- A default block-backed StorageClass for the gateway's SQLite PVC.
- An application namespace (not `default`, `kube-*`, or `openshift-*`).
- Permission to create the cluster-scoped RBAC the gateway chart needs.

Create `gateway-only.yaml` from the template below. It mirrors the gateway half
of what `install.py` renders — `install.py`'s generated values are the reference
implementation of this file. Set the namespace, Route host, and OIDC values to
match Part 2:

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
    issuer: https://keycloak.<apps-domain>/realms/openshell
    audience: openshell-api-openshell-team
    rolesClaim: realm_access.roles
    adminRole: openshell-admin
    userRole: openshell-user
    # caConfigMapName: openshell-oidc-ca  # only when the issuer uses a private CA
pkiInitJob:
  enabled: true
  serverDnsNames:
    - gateway-openshell-team.<apps-domain>
    - openshell.openshell-team.svc
    - openshell.openshell-team.svc.cluster.local
openshiftRoute:
  enabled: true
  host: gateway-openshell-team.<apps-domain>
```

If your issuer's certificate chains to a private CA, create the
`openshell-oidc-ca` ConfigMap in the namespace yourself — the gateway chart
does not create it:

```shell
oc -n openshell-team create configmap openshell-oidc-ca --from-file=ca.crt=issuer.pem
```

Then install the release with an exact chart version:

```shell
helm upgrade --install openshell oci://ghcr.io/nvidia/openshell/helm-chart \
  --version 0.1.3 \
  --namespace openshell-team \
  --values gateway-only.yaml \
  --wait --wait-for-jobs --timeout 10m
```

What you give up compared with the installer: preflight checks, the
cross-namespace OIDC identity reservation (the `openshell-installer` state),
Route-admission waiting, and `uninstall`. Teardown is:

```shell
helm uninstall openshell -n openshell-team
oc -n openshell-team delete secret openshell-server-tls openshell-client-tls openshell-jwt-keys
```

The three Secrets are created at runtime by the chart's certgen hook, outside
the release manifest, so `helm uninstall` never removes them — and they block a
later reinstall if left behind.

Registering the gateway with the CLI (4.1/4.2) works identically after a manual
install.

## Troubleshooting login

Work through these in order — each corresponds to a distinct error page.

### "Client not found" (Keycloak)

The browser was redirected with a `client_id` that does not exist in the realm.
Confirm the `openshell-dashboard-${NS}` client exists in the `openshell` realm
(2.5 or 2.6) and that `oidc.client_id` in your config matches it. The API
audience client (`openshell-api-${NS}`) is referenced only as a token audience;
nobody logs in as it, so its absence produces a different failure, not this page.

### 500 at `/oauth2/callback`: "email ... isn't verified"

oauth2-proxy rejects ID tokens whose `email_verified` claim is `false`. Keycloak
marks manually created users unverified by default. In the admin console, open
**Users → → Details**, toggle **Email verified** On, and **Save**. Repeat for
every user, or set it from the command line:

```shell
KC="https://keycloak.${APPS_DOMAIN}"; REALM="openshell"
AU=$(oc -n keycloak get secret keycloak-initial-admin -o jsonpath='{.data.username}' | base64 -d)
AP=$(oc -n keycloak get secret keycloak-initial-admin -o jsonpath='{.data.password}' | base64 -d)
TOK=$(curl -sk -d client_id=admin-cli -d "username=$AU" --data-urlencode "password=$AP" \
  -d grant_type=password "$KC/realms/master/protocol/openid-connect/token" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
H="Authorization: Bearer $TOK"; UAPI="$KC/admin/realms/$REALM/users"
for uid in $(curl -sk -H "$H" "$UAPI?max=200" \
    | python3 -c "import sys,json;[print(u['id']) for u in json.load(sys.stdin) if not u.get('emailVerified')]"); do
  curl -sk -X PUT -H "$H" -H "Content-Type: application/json" "$UAPI/$uid" -d '{"emailVerified":true}'
done
```

The change takes effect on the next login; no pod restart is needed.

### 403 "You do not have permission to access this resource"

Login succeeded but the user holds no role in `oidc.allowed_roles`, so
oauth2-proxy blocks access (logged as `[AuthFailure] ... unauthorized`). Keep
`allowed_roles` set to **both** `openshell-admin` and `openshell-user` (see the
field table above), then reapply the config from the installer working
directory:

```shell
./install.py install
```

Alternatively, assign one of the allowed roles to the user under **Users →
→ Role mappings**. `allowed_roles` only gates who may reach the dashboard; admin
versus regular privileges are still decided by `admin_role`.

## Cleanup

Remove the OpenShell installation from the installer working directory:

```shell
./install.py uninstall
```

Uninstall asks you to type the namespace name to confirm (or pass `--yes` in
non-interactive use). It removes both Helm releases, the three hook-created TLS
Secrets, the installer-created OIDC Secret and CA ConfigMap, and the
`openshell-installer` state ConfigMap. It intentionally keeps the namespace, and
it preserves administrator-provided Secrets and ConfigMaps — including one named
by `oidc.client_secret_secret` or `oidc.ca_configmap`, even if it reuses a fixed
installer name.

A partial teardown reports every retained resource and exits non-zero instead of
presenting itself as done. Fix the cause and rerun; the state ConfigMap is kept
until every earlier step succeeds so the rerun can prove ownership.

To remove the namespace too, do it explicitly as a separate, optional step:

```shell
oc delete namespace openshell-team
```

Then remove Keycloak and PostgreSQL:

```shell
oc delete keycloak keycloak -n keycloak
oc delete all -l app=keycloak-db -n keycloak
oc delete pvc keycloak-db-data -n keycloak
oc delete secret keycloak-db-secret -n keycloak
oc delete namespace keycloak
```
