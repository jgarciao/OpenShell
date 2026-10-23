#!/usr/bin/env -S uv run --no-project --script
# SPDX-FileCopyrightText: Copyright (c) 2026 OpenShell contributors
# SPDX-License-Identifier: Apache-2.0
# /// script
# requires-python = ">=3.11"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Lifecycle CLI for the OpenShell gateway and dashboard on OpenShift.

Subcommands: init, validate, preflight, install, uninstall. Both components are
always installed together; run every subcommand from the working directory that
holds the config.json produced by init.
"""

import argparse
import base64
import copy
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import yaml

OWNER = "openshell.ai/installer"
OWNER_VALUE = "odh-dashboard"
GATEWAY_HOST = "openshell.ai/installer-gateway-host"
INSTALL_STATE = "openshell-installer"
OIDC_SECRET = "openshell-dashboard-oidc"
OIDC_CA = "openshell-oidc-ca"
TLS_SECRETS = (
    "openshell-server-tls",
    "openshell-client-tls",
    "openshell-jwt-keys",
)
RELEASES = ("openshell", "openshell-dashboard")
VERSION = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
DNS = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")
NAME = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?")

TOOLS = {
    "init": (),
    "validate": ("helm",),
    "preflight": ("helm", "kubectl"),
    "install": ("helm", "kubectl"),
    "uninstall": ("helm", "kubectl"),
}

# Supported RHOAI releases. A profile pins the chart pair and the product image
# tag tested against that build. Chart references live here rather than in the
# README so an OCI move is a one-line change. Never embed image digests: RHOAI
# rebuilds through code freeze and republishes images per z-stream.
RHOAI_PROFILES = {
    "3.6.0": {
        "aliases": ("3.6",),
        "gateway_chart": "oci://ghcr.io/nvidia/openshell/helm-chart",
        "gateway_version": "0.1.3",
        "dashboard_chart": (
            "oci://ghcr.io/gkrumbach07/openshell-dashboard/helm-chart/"
            "openshell-dashboard"
        ),
        "dashboard_version": "0.1.0",
        "image_tag": "rhoai-3.6",
        "prerelease": False,
        "images_qualified": False,
    },
}

# The RHOAI 3.6 CSV ships no oauth2-proxy image, so the profile selects the Red
# Hat hardened image. It is not a per-profile field because no RHOAI build has
# shipped one. The registry is public and needs no pull secret.
AUTH_PROXY_IMAGE = {
    "registry": "registry.access.redhat.com",
    "repository": "hi/oauth2-proxy",
    "tag": "7.15.4",
}
REDHAT_REGISTRY = "registry.redhat.io"

# Map configured chart image components to the RHOAI operator CSV entries that
# declare the image digests for the installed build.
RHOAI_IMAGE_COMPONENTS = (
    ("gateway", "gateway", "RELATED_IMAGE_ODH_OPENSHELL_GATEWAY_IMAGE"),
    ("gateway", "supervisor", "RELATED_IMAGE_ODH_OPENSHELL_SUPERVISOR_IMAGE"),
    ("gateway", "sandboxRuntime", "RELATED_IMAGE_ODH_OPENSHELL_SANDBOX_IMAGE"),
    ("dashboard", "dashboard", "RELATED_IMAGE_ODH_OPENSHELL_DASHBOARD_IMAGE"),
)

# Expose operational chart settings without allowing overlays to replace the
# installer's identity, TLS, service naming, or namespace contract.
COMMON_VALUES = dict.fromkeys(
    ("resources", "nodeSelector", "tolerations", "affinity", "imagePullSecrets")
)
EXTRA_VALUES = {
    "gateway": {
        **COMMON_VALUES,
        "server": dict.fromkeys(
            (
                "workspaceStorageClass",
                "workspaceDefaultStorageSize",
                "defaultRuntimeClassName",
                "sandboxImagePullSecrets",
            )
        ),
        "openshiftRoute": {"annotations": None},
    },
    "dashboard": {
        **COMMON_VALUES,
        "authProxy": {
            "resources": None,
            "cookie": {"refresh": None},
            "upstreamTimeout": None,
        },
        "networkPolicy": {"ingressNamespace": None, "ingressFrom": None},
        "openshiftRoute": {"annotations": None},
    },
}

# The templates are embedded because an administrator downloads only install.py.
# They are kept byte-identical to the .example files beside it by a test.
CONFIG_TEMPLATE = """\
{
  "namespace": "openshell-team",
  "gateway": {
    "chart": "oci://ghcr.io/nvidia/openshell/helm-chart",
    "version": "REPLACE_WITH_EXACT_OPENSHELL_VERSION",
    "helm_overrides": ["gateway-overrides.yaml"],
    "host": "gateway.apps.example.com"
  },
  "dashboard": {
    "chart": "oci://ghcr.io/gkrumbach07/openshell-dashboard/helm-chart/openshell-dashboard",
    "version": "REPLACE_WITH_EXACT_OPENSHELL_DASHBOARD_VERSION",
    "helm_overrides": ["dashboard-overrides.yaml"],
    "host": "dashboard.apps.example.com"
  },
  "oidc": {
    "provider": "oidc",
    "issuer": "https://identity.example.com/realms/customer",
    "client_id": "openshell-dashboard-openshell-team",
    "client_secret_file": "openshell-dashboard-oidc-secret.txt",
    "audience": "openshell-api-openshell-team",
    "roles_claim": "roles",
    "admin_role": "openshell-admin",
    "user_role": "openshell-user",
    "allowed_groups": [],
    "allowed_roles": []
  }
}
"""

GATEWAY_OVERRIDES_TEMPLATE = """\
# SPDX-FileCopyrightText: Copyright (c) 2026 OpenShell contributors
# SPDX-License-Identifier: Apache-2.0
# Optional operational overrides. config.json loads this file through
# gateway.helm_overrides. Use sizes appropriate for your workload.
resources:
  requests:
    cpu: 100m
    memory: 256Mi
# imagePullSecrets:
#   - name: registry-credentials
# server:
#   workspaceStorageClass: block-storage
#   sandboxImagePullSecrets:
#     - name: registry-credentials
"""

DASHBOARD_OVERRIDES_TEMPLATE = """\
# SPDX-FileCopyrightText: Copyright (c) 2026 OpenShell contributors
# SPDX-License-Identifier: Apache-2.0
# Optional operational overrides, loaded through dashboard.helm_overrides.
resources:
  requests:
    cpu: 100m
    memory: 128Mi
authProxy:
  resources:
    requests:
      cpu: 50m
      memory: 64Mi
# For custom/host-network routers, set networkPolicy.ingressFrom to peers
# reviewed for your ingress controller. Keep the proxy restricted to the router.
"""

TEMPLATE_CONFIG = json.loads(CONFIG_TEMPLATE)
SHARED_OIDC_FIELDS = (
    "provider",
    "issuer",
    "ca_file",
    "ca_configmap",
    "roles_claim",
    "admin_role",
    "user_role",
    "scope",
)


def merge_values(target, overlay, allowed, prefix=""):
    require(isinstance(overlay, dict), f"{prefix or 'Values'} must be a mapping")
    for key, value in overlay.items():
        require(
            key in allowed, f"Unsupported or installer-owned Helm value: {prefix}{key}"
        )
        if allowed[key] is not None:
            merge_values(
                target.setdefault(key, {}), value, allowed[key], f"{prefix}{key}."
            )
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            # Helm's map merge semantics; arrays/scalars are replaced.
            merge_unrestricted(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def merge_unrestricted(target, overlay):
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_unrestricted(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def fields(obj, required, optional=()):
    require(isinstance(obj, dict), "Configuration sections must be JSON objects")
    require(
        not (set(obj) - set(required) - set(optional)),
        f"Unknown configuration fields: {set(obj) - set(required) - set(optional)}",
    )
    for key in required:
        require(key in obj and obj[key] != "", f"Missing configuration field: {key}")


def valid_dns_name(name):
    return (
        isinstance(name, str)
        and len(name) <= 253
        and DNS.fullmatch(name) is not None
        and all(
            0 < len(label) <= 63
            and not label.startswith("-")
            and not label.endswith("-")
            for label in name.split(".")
        )
    )


def validate_namespace(namespace):
    require(
        isinstance(namespace, str)
        and len(namespace) <= 63
        and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", namespace) is not None,
        "Invalid namespace",
    )
    require(
        namespace not in ("default", "openshift", "kube-system", "kube-public")
        and not namespace.startswith(("kube-", "openshift-")),
        "Choose an application namespace, not a Kubernetes/OpenShift system namespace",
    )


def validate_issuer(issuer):
    parts = urlsplit(issuer)
    require(
        parts.scheme == "https"
        and parts.hostname
        and not parts.username
        and not parts.password
        and not parts.query
        and not parts.fragment,
        "OIDC issuer must be an HTTPS URL",
    )


def validate_scope(scope):
    require(
        isinstance(scope, str)
        and re.fullmatch(
            r"[\x21\x23-\x5B\x5D-\x7E]+(?: [\x21\x23-\x5B\x5D-\x7E]+)*",
            scope,
        )
        and "openid" in scope.split(),
        "oidc.scope must be space-separated OAuth scopes including openid",
    )


def validate_shared_oidc(oidc):
    require(isinstance(oidc, dict), "oidc must be a JSON object")
    for key in ("issuer", "roles_claim", "admin_role", "user_role"):
        require(
            isinstance(oidc.get(key), str) and oidc[key].strip(),
            f"Missing oidc.{key} in --from-config source",
        )
    validate_issuer(oidc["issuer"])
    require(
        oidc["admin_role"] != oidc["user_role"],
        "Admin and user roles must be distinct",
    )
    provider = oidc.get("provider", "oidc")
    require(provider in ("oidc", "keycloak-oidc"), "Unsupported OIDC provider")
    require(
        not oidc.get("allowed_roles") or provider == "keycloak-oidc",
        "allowed_roles requires keycloak-oidc; use allowed_groups for generic OIDC",
    )
    if "scope" in oidc:
        validate_scope(oidc["scope"])


def load_config(path, namespace=None):
    return validate_config(json.loads(path.read_text()), path.parent, namespace)


def validate_config(config, base_dir, namespace=None, require_files=True):
    require(isinstance(config, dict), "Configuration must be a JSON object")
    # Migration guards run before fields() so stale configs get a real message
    # instead of the generic unknown-key error.
    require(
        "context" not in config,
        "context is no longer a configuration field; pass --context or rely on "
        "your current kubectl context",
    )
    for name in ("gateway", "dashboard"):
        item = config.get(name)
        if isinstance(item, dict):
            require(
                "values_files" not in item,
                f"{name}.values_files was renamed to helm_overrides",
            )
    if namespace is not None:
        config["namespace"] = namespace
    fields(
        config,
        ("namespace", "gateway", "dashboard", "oidc"),
        (
            "agent_sandbox_namespace",
            "agent_sandbox_deployment",
            "rhods_operator_namespace",
        ),
    )
    validate_namespace(config["namespace"])
    for key in (
        "agent_sandbox_namespace",
        "agent_sandbox_deployment",
        "rhods_operator_namespace",
    ):
        if key in config:
            require(
                isinstance(config[key], str) and DNS.fullmatch(config[key]),
                f"Invalid {key}",
            )
    base_dir = Path(base_dir)
    for name in ("gateway", "dashboard"):
        item = config[name]
        fields(item, ("chart", "version", "host"), ("images", "helm_overrides"))
        require(
            isinstance(item["chart"], str) and item["chart"].startswith("oci://"),
            f"{name}.chart must be an OCI chart reference",
        )
        require(
            isinstance(item["version"], str) and VERSION.fullmatch(item["version"]),
            f"{name}.version must be exact SemVer (no latest or version ranges)",
        )
        require(
            item["version"] != "0.0.0",
            "Use a published release or pinned development version",
        )
        sources = item.get("helm_overrides", [])
        require(
            isinstance(sources, list)
            and all(isinstance(p, str) and p for p in sources),
            f"{name}.helm_overrides must be a list of file paths",
        )
        overrides = {}
        for source in sources:
            path = base_dir / source
            # SafeLoader does not construct Python objects. Do not include file
            # contents in diagnostics: user-supplied files may contain secrets.
            try:
                overlay = yaml.safe_load(path.read_text())
            except yaml.YAMLError:
                raise ValueError(f"Invalid YAML in {name} values file") from None
            merge_values(overrides, overlay, EXTRA_VALUES[name])
        item["values"] = overrides
        require(valid_dns_name(item["host"]), f"Invalid {name}.host")
        components = (
            ("gateway", "supervisor", "sandboxRuntime", "sandbox")
            if name == "gateway"
            else ("dashboard", "authProxy")
        )
        fields(item.get("images", {}), (), components)
        for component, image in item.get("images", {}).items():
            keys = (
                ("repository", "tag", "digest")
                if component == "sandbox"
                else ("registry", "repository", "tag", "digest")
            )
            fields(image, (), keys)
            require(
                all(isinstance(value, str) for value in image.values()),
                "Image values must be strings",
            )
    require(
        config["gateway"]["host"] != config["dashboard"]["host"],
        "Gateway and dashboard need distinct hosts",
    )
    oidc = config["oidc"]
    fields(
        oidc,
        ("issuer", "client_id", "audience", "roles_claim", "admin_role", "user_role"),
        (
            "provider",
            "allowed_groups",
            "allowed_roles",
            "client_secret_file",
            "client_secret_secret",
            "ca_file",
            "ca_configmap",
            "scope",
        ),
    )
    for key in (
        "issuer",
        "client_id",
        "audience",
        "roles_claim",
        "admin_role",
        "user_role",
    ):
        require(
            isinstance(oidc[key], str) and oidc[key].strip(),
            f"oidc.{key} must be a nonempty string",
        )
    validate_issuer(oidc["issuer"])
    require(
        oidc["client_id"] != oidc["audience"],
        "Browser client ID and gateway audience must be distinct",
    )
    require(
        oidc["admin_role"] != oidc["user_role"], "Admin and user roles must be distinct"
    )
    require(
        oidc.get("provider", "oidc") in ("oidc", "keycloak-oidc"),
        "Unsupported OIDC provider",
    )
    for key in ("allowed_groups", "allowed_roles"):
        require(
            isinstance(oidc.get(key, []), list)
            and all(isinstance(v, str) and v for v in oidc.get(key, [])),
            f"oidc.{key} must be a list of strings",
        )
    require(
        not oidc.get("allowed_roles") or oidc.get("provider") == "keycloak-oidc",
        "allowed_roles requires keycloak-oidc; use allowed_groups for generic OIDC",
    )
    if "scope" in oidc:
        validate_scope(oidc["scope"])
    require(
        not (oidc.get("client_secret_file") and oidc.get("client_secret_secret")),
        "oidc.client_secret_file and oidc.client_secret_secret are mutually exclusive",
    )
    require(
        not (oidc.get("ca_file") and oidc.get("ca_configmap")),
        "oidc.ca_file and oidc.ca_configmap are mutually exclusive",
    )
    for key in ("client_secret_secret", "ca_configmap"):
        if oidc.get(key):
            require(
                isinstance(oidc[key], str)
                and len(oidc[key]) <= 253
                and NAME.fullmatch(oidc[key]) is not None,
                f"Invalid oidc.{key}",
            )
    for key in ("client_secret_file", "ca_file"):
        if oidc.get(key):
            require(
                isinstance(oidc[key], str) and oidc[key].strip(),
                f"oidc.{key} must be a nonempty string",
            )
            if require_files:
                source = (base_dir / oidc[key]).resolve()
                require(
                    source.is_file() and source.stat().st_size > 0,
                    f"Missing or empty oidc.{key}",
                )
                oidc[key] = str(source)
    return config


def chart_values(config):
    ns, oidc = config["namespace"], config["oidc"]
    ca = oidc.get("ca_configmap") or (OIDC_CA if oidc.get("ca_file") else "")
    security = {"runAsUser": None, "seccompProfile": {"type": "RuntimeDefault"}}
    gateway = {
        "fullnameOverride": "openshell",
        "podSecurityContext": None,
        "securityContext": security,
        "replicaCount": 1,
        "server": {
            "disableTls": False,
            "sandboxNamespace": ns,
            "auth": {"allowUnauthenticatedUsers": False},
            "tls": {
                "enableMtls": False,
                "clientCaSecretName": "",
                "certSecretName": "openshell-server-tls",
                "clientTlsSecretName": "openshell-client-tls",
            },
            "oidc": {
                "issuer": oidc["issuer"],
                "audience": oidc["audience"],
                "rolesClaim": oidc["roles_claim"],
                "adminRole": oidc["admin_role"],
                "userRole": oidc["user_role"],
                "caConfigMapName": ca,
            },
        },
        "pkiInitJob": {
            "enabled": True,
            "serverDnsNames": [
                config["gateway"]["host"],
                f"openshell.{ns}.svc",
                f"openshell.{ns}.svc.cluster.local",
            ],
        },
        "openshiftRoute": {"enabled": True, "host": config["gateway"]["host"]},
    }
    dashboard = {
        "fullnameOverride": "openshell-dashboard",
        "podSecurityContext": None,
        "securityContext": security,
        "gateway": {
            "url": f"grpcs://openshell.{ns}.svc.cluster.local:8080",
            "caSecretName": "openshell-client-tls",
        },
        "oidc": {
            "provider": oidc.get("provider", "oidc"),
            "issuer": oidc["issuer"],
            "clientId": oidc["client_id"],
            "existingSecret": oidc.get("client_secret_secret") or OIDC_SECRET,
            "audience": oidc["audience"],
            "rolesClaim": oidc["roles_claim"],
            "adminRole": oidc["admin_role"],
            "codeChallengeMethod": "S256",
            "allowedGroups": oidc.get("allowed_groups", []),
            "allowedRoles": oidc.get("allowed_roles", []),
            "caConfigMapName": ca,
            "scope": oidc.get("scope", ""),
        },
        "openshiftRoute": {
            "enabled": True,
            "host": config["dashboard"]["host"],
            "termination": "edge",
        },
        "networkPolicy": {"ingressNamespace": "openshift-ingress"},
    }
    for name, values in (("gateway", gateway), ("dashboard", dashboard)):
        merge_values(values, config[name].get("values", {}), EXTRA_VALUES[name])
        for component, image in config[name].get("images", {}).items():
            values.setdefault(component, {})["image"] = image
    return gateway, dashboard


def image_repository(image):
    registry = image.get("registry", "")
    repository = image["repository"]
    return f"{registry}/{repository}" if registry else repository


def related_image_name(name):
    """Normalize a CSV relatedImage name to ODH_OPENSHELL_<COMPONENT>_IMAGE.

    RHOAI CSVs use different spellings: `RELATED_IMAGE_ODH_OPENSHELL_GATEWAY_IMAGE`
    in one build and `odh_openshell_gateway_image` in another. Normalize case,
    separators, and the optional prefix before comparing.
    """
    return (
        str(name)
        .upper()
        .replace("-", "_")
        .replace(".", "_")
        .removeprefix("RELATED_IMAGE_")
    )


def split_image(reference):
    """Return (repository, digest-or-tag) for a full image reference."""
    if "@" in reference:
        return tuple(reference.split("@", 1))
    last = reference.rsplit("/", 1)[-1]
    if ":" in last:
        return tuple(reference.rsplit(":", 1))
    return reference, None


class Installer:
    def __init__(self, config, timeout, context=None):
        self.config = config
        self.timeout = timeout
        self.context = context
        self.context_args = ["--context", context] if context else []
        self.kube = ["kubectl", *self.context_args, "--request-timeout=30s"]
        self.ns = config["namespace"]
        self.warnings = []
        self.passed = 0

    def section(self, title):
        print(f"\n{title}")

    def emit(self, level, message):
        print(f"  [{level}] {message}")
        if level == "ok":
            self.passed += 1
        elif level == "warn":
            self.warnings.append(message)

    def helm_args(self, *args):
        # Single choke point for cluster-facing helm calls. helm pull and
        # helm template stay literal: they are local and must never receive
        # --kube-context.
        return [
            "helm",
            *args,
            *(["--kube-context", self.context] if self.context else []),
        ]

    def audit_context(self):
        # Ambient context must be visible: an audit line, not a prompt.
        view = json.loads(self.run(["kubectl", "config", "view", "-o", "json"]))
        name = self.context or view.get("current-context") or ""
        require(name, "No kubectl context is set; pass --context or run oc login")
        entry = next(
            (item for item in view.get("contexts") or [] if item.get("name") == name),
            None,
        )
        require(entry, f"kubectl context {name} is not in the kubeconfig")
        cluster = entry.get("context", {}).get("cluster") or ""
        detail = f" (cluster {cluster})" if cluster else ""
        print(f"kubectl context: {name}{detail}")

    def audit_identity(self):
        oidc = self.config["oidc"]
        callback = f"https://{self.config['dashboard']['host']}/oauth2/callback"
        print(
            f"Target namespace: {self.ns}\n"
            f"OIDC identity: issuer {oidc['issuer']}, audience {oidc['audience']}, "
            f"client {oidc['client_id']}, callback {callback}"
        )

    def run(self, args, payload=None):
        # Never log stdin: it can contain Secret data. Do not echo commands.
        result = subprocess.run(
            args, input=payload, text=True, capture_output=True, check=False
        )
        if result.returncode:
            raise RuntimeError(f"{args[0]} failed: {result.stderr.strip()}")
        return result.stdout

    def get(self, kind, name, namespaced=True):
        args = self.kube + (["-n", self.ns] if namespaced else [])
        output = self.run(
            [*args, "get", kind, name, "--ignore-not-found", "-o", "json"]
        )
        return json.loads(output) if output.strip() else None

    def create(self, resource):
        self.run([*self.kube, "create", "-f", "-"], json.dumps(resource))

    def release_has_gateway(self, release):
        # Chart names and images can change in downstream packages. Inspect the
        # stored workload instead, including failed releases with no running pod.
        manifest = self.run(
            self.helm_args("get", "manifest", release, "--namespace", self.ns)
        )
        try:
            for resource in yaml.safe_load_all(manifest):
                if not isinstance(resource, dict):
                    continue
                if resource.get("kind") == "Pod":
                    pod = resource
                elif resource.get("kind") in ("Deployment", "StatefulSet", "DaemonSet"):
                    pod = resource.get("spec", {}).get("template", {})
                else:
                    continue
                for container in pod.get("spec", {}).get("containers", []):
                    if container.get("name") == "openshell-gateway" or any(
                        Path(arg).name == "openshell-gateway"
                        for arg in container.get("command", [])
                    ):
                        return True
        except yaml.YAMLError:
            # Helm manifests can contain Secrets; never include parser excerpts.
            raise ValueError(f"Cannot inspect manifest for release {release}") from None
        return False

    def check_releases(self, managed):
        # Explicit status flags work with both Helm 3 and Helm 4. Paginate so a
        # busy administrator-managed namespace cannot hide a conflicting release.
        offset = 0
        while True:
            releases = json.loads(
                self.run(
                    self.helm_args(
                        "list",
                        "--namespace",
                        self.ns,
                        "--output",
                        "json",
                        "--max",
                        "256",
                        "--offset",
                        str(offset),
                        "--deployed",
                        "--failed",
                        "--pending",
                        "--uninstalling",
                        "--uninstalled",
                        "--superseded",
                    )
                )
            )
            for release in releases:
                name = release["name"]
                # --keep-history leaves an uninstalled record, not a gateway.
                # Keep fixed-name ownership checks: Helm still owns that history.
                if release.get("status") == "uninstalled" and name not in RELEASES:
                    continue
                require(
                    managed or name not in RELEASES,
                    f"Release {name} already exists outside this installer; migrate it explicitly",
                )
                if name != "openshell":
                    gateway = release.get("chart", "").startswith(
                        "helm-chart-"
                    ) or self.release_has_gateway(name)
                    require(
                        not gateway,
                        f"Namespace {self.ns} already has gateway release {name}; use one gateway per namespace",
                    )
            if len(releases) < 256:
                break
            offset += len(releases)

    def cluster_checks(self, namespace):
        version = self.get("clusterversion.config.openshift.io", "version", False)
        self.emit(
            "ok",
            "OpenShift "
            + str(
                (version or {})
                .get("status", {})
                .get("desired", {})
                .get("version", "unknown")
            ),
        )
        storage = json.loads(
            self.run([*self.kube, "get", "storageclasses", "-o", "json"])
        )
        defaults = [
            item["metadata"]["name"]
            for item in storage["items"]
            if any(
                item["metadata"].get("annotations", {}).get(key) == "true"
                for key in (
                    "storageclass.kubernetes.io/is-default-class",
                    "storageclass.beta.kubernetes.io/is-default-class",
                )
            )
        ]
        require(
            defaults,
            "No default StorageClass; the gateway chart's SQLite PVC requires one",
        )
        self.emit("ok", f"Default StorageClass: {', '.join(defaults)}")
        self.emit(
            "info",
            "Use block-backed storage for the gateway SQLite PVC.",
        )
        custom = (
            self.config["gateway"]
            .get("values", {})
            .get("server", {})
            .get("workspaceStorageClass")
        )
        if custom:
            require(
                any(item["metadata"]["name"] == custom for item in storage["items"]),
                "Configured workspaceStorageClass does not exist",
            )
            self.emit("ok", f"Workspace StorageClass exists: {custom}")
        nodes = json.loads(self.run([*self.kube, "get", "nodes", "-o", "json"]))
        kernels = sorted(
            {
                item.get("status", {})
                .get("nodeInfo", {})
                .get("kernelVersion", "unknown")
                for item in nodes["items"]
            }
        )
        self.emit("info", f"Node kernels: {', '.join(kernels) or 'none reported'}")
        if any(
            (match := re.match(r"(\d+)\.(\d+)", kernel))
            and tuple(map(int, match.groups())) < (5, 19)
            for kernel in kernels
        ):
            self.emit(
                "info",
                "Kernels before 5.19 need a build with the legacy-kernel fixes. "
                "No action is required unless sandbox networking fails.",
            )
        # Check actual installation operations instead of requiring wildcard
        # cluster-admin. These checks cannot prove RBAC bind/escalate or SCC admission.
        scoped = [
            "secrets",
            "configmaps",
            "services",
            "serviceaccounts",
            "deployments.apps",
            "statefulsets.apps",
            "jobs.batch",
            "roles.rbac.authorization.k8s.io",
            "rolebindings.rbac.authorization.k8s.io",
            "routes.route.openshift.io",
            "networkpolicies.networking.k8s.io",
        ]
        operations = [
            (verb, resource, True)
            for resource in scoped
            for verb in ("create", "patch")
        ]
        # Helm updates its release Secret to record installation/upgrade status.
        # --history-max also makes Helm delete older release Secrets.
        operations += [("update", "secrets", True), ("delete", "secrets", True)]
        operations += [
            (verb, resource, False)
            for resource in (
                "clusterroles.rbac.authorization.k8s.io",
                "clusterrolebindings.rbac.authorization.k8s.io",
            )
            for verb in ("create", "patch")
        ]
        if not namespace:
            operations.append(("create", "namespaces", False))
        for verb, resource, namespaced in operations:
            try:
                answer = self.run(
                    [
                        *self.kube,
                        "auth",
                        "can-i",
                        verb,
                        resource,
                        *(["-n", self.ns] if namespaced else []),
                    ]
                )
                require(answer.strip() == "yes", "denied")
            except (RuntimeError, ValueError):
                raise ValueError(
                    f"Preflight permission check failed: {verb} {resource}"
                    + (f" in {self.ns}" if namespaced else " cluster-wide")
                ) from None
        self.emit("ok", f"Installation permissions checked ({len(operations)})")

    def check_route_hosts(self):
        # A host claimed in another namespace fails at Route admission, after
        # both releases already exist. Catch it before any mutation.
        routes = json.loads(
            self.run([*self.kube, "get", "routes", "--all-namespaces", "-o", "json"])
        )
        for route in routes.get("items", []):
            owner = route.get("metadata", {}).get("namespace")
            if owner == self.ns:
                continue
            host = route.get("spec", {}).get("host")
            for component in ("gateway", "dashboard"):
                desired = self.config[component]["host"]
                require(
                    host != desired,
                    f"Host {desired} is already claimed by Route "
                    f"{route.get('metadata', {}).get('name')} in namespace {owner}",
                )
        self.emit("ok", "Route hosts are not claimed by another namespace")

    def installed_images(self):
        return [
            (name, component, self.config[name].get("images", {}).get(component))
            for name, component, _ in RHOAI_IMAGE_COMPONENTS
        ]

    def verify_rhoai_images(self):
        configured = [item for item in self.installed_images() if item[2]]
        if not configured:
            self.emit(
                "info",
                "No product images configured; the charts' upstream defaults will be used.",
            )
            return
        namespace = self.config.get("rhods_operator_namespace", "redhat-ods-operator")
        try:
            output = self.run([*self.kube, "-n", namespace, "get", "csv", "-o", "json"])
        except (RuntimeError, ValueError):
            self.emit(
                "warn",
                "Cannot read the RHOAI operator CSV; product images were not "
                "verified against this cluster's operator build.",
            )
            return
        csvs = [
            item
            for item in json.loads(output).get("items", [])
            if any(
                "OPENSHELL" in related_image_name(related.get("name", ""))
                and related_image_name(related.get("name", "")).endswith("_IMAGE")
                for related in item.get("spec", {}).get("relatedImages", [])
            )
        ]
        if not csvs:
            self.emit(
                "warn",
                f"No RHOAI operator CSV found in {namespace}; product images were "
                "not verified.",
            )
            return
        require(
            len(csvs) == 1,
            "More than one OpenShell-bearing CSV in "
            f"{namespace}: {', '.join(item['metadata']['name'] for item in csvs)}",
        )
        csv_name = csvs[0]["metadata"]["name"]
        declared = {
            related_image_name(related.get("name", "")): related.get("image", "")
            for related in csvs[0].get("spec", {}).get("relatedImages", [])
        }
        unresolved = []
        skopeo = shutil.which("skopeo")
        unauthorized = False
        for name, component, mapped in RHOAI_IMAGE_COMPONENTS:
            image = self.config[name].get("images", {}).get(component)
            if not image:
                continue
            key = related_image_name(mapped)
            reference = declared.get(key)
            if not reference:
                self.emit("warn", f"The RHOAI CSV does not declare {mapped}")
                continue
            expected_repository, expected_digest = split_image(reference)
            repository = image_repository(image)
            require(
                repository == expected_repository,
                f"{name}.images.{component} repository {repository} does not match "
                f"the RHOAI operator CSV image {expected_repository}",
            )
            if not expected_digest:
                self.emit(
                    "warn",
                    f"The RHOAI CSV declares no digest for {mapped}; "
                    f"{repository} was not verified.",
                )
                continue
            if image.get("digest"):
                require(
                    image["digest"] == expected_digest,
                    f"{name}.images.{component} digest does not match the RHOAI "
                    f"operator CSV digest {expected_digest}",
                )
                continue
            require(
                image.get("tag"), f"{name}.images.{component} needs a tag or digest"
            )
            if not skopeo:
                unresolved.append(f"{repository}:{image['tag']}")
                continue
            result = subprocess.run(
                ["skopeo", "inspect", f"docker://{repository}:{image['tag']}"],
                text=True,
                capture_output=True,
                check=False,
            )
            try:
                resolved = json.loads(result.stdout).get("Digest")
            except json.JSONDecodeError:
                resolved = None
            if result.returncode or not resolved:
                unauthorized = (
                    unauthorized or "unauthorized" in (result.stderr or "").lower()
                )
                unresolved.append(f"{repository}:{image['tag']}")
                continue
            require(
                resolved == expected_digest,
                f"{name}.images.{component} tag {image['tag']} resolves to {resolved}; "
                f"the RHOAI operator CSV declares {expected_digest}",
            )
        self.emit(
            "ok",
            f"Product image repositories match the RHOAI operator CSV ({csv_name})",
        )
        if unresolved:
            if not skopeo:
                reason = "skopeo is not installed"
                remedy = "install skopeo to verify tag digests"
            elif unauthorized:
                reason = "skopeo is not authorized by the registry"
                remedy = "run `skopeo login registry.redhat.io`"
            else:
                reason = "skopeo could not resolve the tags"
                remedy = "check registry access"
            self.emit(
                "warn",
                f"Digests of {len(unresolved)} tag-based image(s) were not verified "
                f"({reason}). Repositories matched the operator CSV. To verify "
                f"digests, {remedy} or pin images.*.digest.",
            )

    def identity_state(self):
        oidc = self.config["oidc"]
        secret_source = "external" if oidc.get("client_secret_secret") else "installer"
        if oidc.get("ca_configmap"):
            ca_source = "external"
        elif oidc.get("ca_file"):
            ca_source = "installer"
        else:
            ca_source = ""
        return {
            "gateway-host": self.config["gateway"]["host"],
            **{
                f"oidc-{key.replace('_', '-')}": oidc[key]
                for key in ("issuer", "audience", "client_id")
            },
            "oidc-secret-source": secret_source,
            "oidc-ca-source": ca_source,
        }

    def check_identity(self, state):
        desired = self.identity_state()
        for key, value in (state or {}).get("data", {}).items():
            if key in desired:
                require(
                    value == desired[key],
                    f"Installer identity {key} changed; explicit identity/certificate migration is required",
                )
        states = json.loads(
            self.run(
                [
                    *self.kube,
                    "get",
                    "configmaps",
                    "--all-namespaces",
                    "-l",
                    f"{OWNER}={OWNER_VALUE}",
                    "-o",
                    "json",
                ]
            )
        )
        for other in states["items"]:
            if (
                other["metadata"]["name"] != INSTALL_STATE
                or other["metadata"].get("namespace") == self.ns
            ):
                continue
            data = other.get("data", {})
            if "oidc-issuer" not in data:
                print(
                    f"Note: legacy installer state in {other['metadata']['namespace']} has no identity metadata; verify audience isolation manually."
                )
                continue
            if data["oidc-issuer"].rstrip("/") == desired["oidc-issuer"].rstrip("/"):
                require(
                    data.get("oidc-audience") != desired["oidc-audience"]
                    and data.get("oidc-client-id") != desired["oidc-client-id"],
                    f"OIDC audience/browser client already used by namespace {other['metadata']['namespace']}; provision distinct per-gateway registrations",
                )

    def preflight(self):
        self.warnings = []
        self.passed = 0
        print(f"Preflight for namespace {self.ns}")
        self.section("Cluster")
        self.run([*self.kube, "get", "--raw", "/apis/route.openshift.io/v1"])
        self.emit("ok", "OpenShift Route API is available")
        namespace = self.get("namespace", self.ns, False)
        require(
            not namespace or namespace.get("status", {}).get("phase") != "Terminating",
            "Namespace is terminating",
        )
        if namespace:
            self.emit("ok", f"Namespace {self.ns} exists")
        else:
            self.emit(
                "info",
                f"Namespace {self.ns} does not exist yet; install will create it",
            )
        self.cluster_checks(namespace)

        self.section("Agent Sandbox")
        crd = self.get("crd", "sandboxes.agents.x-k8s.io", False)
        served = [
            v["name"]
            for v in (crd or {}).get("spec", {}).get("versions", [])
            if v["served"]
        ]
        require(
            any(name in ("v1alpha1", "v1beta1") for name in served),
            "Install the Agent Sandbox CRDs and controller first",
        )
        self.emit("ok", f"Agent Sandbox CRD is served ({', '.join(served)})")
        controller = self.config.get(
            "agent_sandbox_deployment", "agent-sandbox-controller"
        )
        self.run(
            [
                *self.kube,
                "-n",
                self.config.get("agent_sandbox_namespace", "agent-sandbox-system"),
                "rollout",
                "status",
                "deployment/" + controller,
                "--timeout",
                self.timeout,
            ]
        )
        self.emit("ok", f"Agent Sandbox controller {controller} is ready")

        self.section("RHOAI product images")
        self.verify_rhoai_images()

        self.section("Installer state, identity, and networking")
        self.check_route_hosts()
        state = self.get("configmap", INSTALL_STATE) if namespace else None
        require(
            not state or state["metadata"].get("labels", {}).get(OWNER) == OWNER_VALUE,
            f"ConfigMap {INSTALL_STATE} already exists outside this installer",
        )
        # Preserve compatibility with installations made before state moved off
        # the Namespace. Never modify administrator-owned Namespace metadata.
        legacy_host = (
            (namespace or {})
            .get("metadata", {})
            .get("annotations", {})
            .get(GATEWAY_HOST)
        )
        original_host = (
            state.get("data", {}).get("gateway-host") if state else legacy_host
        )
        require(
            not (state or legacy_host)
            or original_host == self.config["gateway"]["host"],
            "Gateway hostname differs from the original install; certificate migration is required",
        )
        if state or legacy_host:
            self.emit("ok", "Existing installer state matches this configuration")
        self.check_identity(state)
        self.emit("ok", "OIDC identity is distinct from other installations")
        if namespace:
            self.check_releases(managed=bool(state or legacy_host))
            self.emit("ok", "No conflicting Helm releases in the namespace")
            # The chart's bootstrap hook reuses existing TLS material. Do not
            # accidentally use certificates belonging to another gateway.
            if not (state or legacy_host):
                for secret in TLS_SECRETS:
                    require(
                        not self.get("secret", secret),
                        f"Existing gateway Secret {secret} requires explicit migration",
                    )
                self.emit("ok", "No pre-existing gateway TLS Secrets")
        oidc = self.config["oidc"]
        require(
            state
            or legacy_host
            or oidc.get("client_secret_file")
            or oidc.get("client_secret_secret"),
            "oidc.client_secret_file or oidc.client_secret_secret is required for "
            "the first install",
        )
        self.credentials(check_only=True, namespace_exists=bool(namespace))
        self.emit("ok", "Client credentials are valid")
        print(
            f"\nPreflight checks: {self.passed} passed, "
            f"{len(self.warnings)} warning(s)."
        )
        return namespace

    def credentials(self, check_only=False, *, namespace_exists=True):
        require(
            namespace_exists or check_only, "Create the namespace before credentials"
        )
        oidc = self.config["oidc"]
        external = oidc.get("client_secret_secret")
        if external:
            # Administrator-managed: validate the required keys and use it as-is.
            # Never create, label, or compare it against a local file.
            secret = self.get("secret", external) if namespace_exists else None
            if namespace_exists:
                require(secret, f"Secret {external} was not found")
                data = secret.get("data", {})
                require(
                    data.get("client-secret") and data.get("cookie-secret"),
                    f"Secret {external} is missing client-secret or cookie-secret",
                )
        else:
            existing = self.get("secret", OIDC_SECRET) if namespace_exists else None
            supplied = (
                Path(oidc["client_secret_file"]).read_bytes().rstrip(b"\r\n")
                if oidc.get("client_secret_file")
                else None
            )
            if supplied is not None:
                require(supplied, "Client secret file contains no secret")
            if existing:
                data = existing.get("data", {})
                require(
                    data.get("client-secret") and data.get("cookie-secret"),
                    "Existing OIDC Secret is missing required keys",
                )
                require(
                    supplied is None
                    or base64.b64decode(data["client-secret"]) == supplied,
                    "OIDC Secret differs from file; rotate it explicitly before rerunning",
                )
            else:
                require(supplied, "Client secret file is required")
                if not check_only:
                    self.create(
                        {
                            "apiVersion": "v1",
                            "kind": "Secret",
                            "type": "Opaque",
                            "metadata": {
                                "name": OIDC_SECRET,
                                "namespace": self.ns,
                                "labels": {OWNER: OWNER_VALUE},
                            },
                            "data": {
                                "client-secret": base64.b64encode(supplied).decode(),
                                "cookie-secret": base64.b64encode(
                                    secrets.token_urlsafe(32).encode()
                                ).decode(),
                            },
                        }
                    )
        if oidc.get("ca_configmap"):
            # Administrator-managed CA ConfigMap: never owned or modified.
            if namespace_exists:
                require(
                    self.get("configmap", oidc["ca_configmap"]),
                    f"ConfigMap {oidc['ca_configmap']} was not found",
                )
        elif oidc.get("ca_file"):
            ca = Path(oidc["ca_file"]).read_text()
            existing_ca = self.get("configmap", OIDC_CA) if namespace_exists else None
            require(
                not existing_ca or existing_ca.get("data", {}).get("ca.crt") == ca,
                "OIDC CA differs; rotate the ConfigMap and restart both releases explicitly",
            )
            if not existing_ca and not check_only:
                self.create(
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {
                            "name": OIDC_CA,
                            "namespace": self.ns,
                            "labels": {OWNER: OWNER_VALUE},
                        },
                        "data": {"ca.crt": ca},
                    }
                )

    def wait_for_route(self, release):
        component = "gateway" if release == "openshell" else "dashboard"
        host = self.config[component]["host"]
        seconds = (
            int(self.timeout[:-1]) * {"s": 1, "m": 60, "h": 3600}[self.timeout[-1]]
        )
        deadline = time.monotonic() + seconds
        while True:
            route = self.get("route", release)
            if route and any(
                condition.get("type") == "Admitted"
                and condition.get("status") == "True"
                for ingress in route.get("status", {}).get("ingress", [])
                if ingress.get("host") == host
                for condition in ingress.get("conditions", [])
            ):
                return
            require(
                time.monotonic() < deadline,
                f"Route {release} is not admitted; inspect it and rerun",
            )
            time.sleep(min(2, max(0, deadline - time.monotonic())))

    def install(self, charts, values):
        namespace = self.preflight()
        if not namespace:
            self.create(
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {
                        "name": self.ns,
                        "labels": {OWNER: OWNER_VALUE},
                    },
                }
            )
        state = self.get("configmap", INSTALL_STATE)
        if not state:
            self.create(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {
                        "name": INSTALL_STATE,
                        "namespace": self.ns,
                        "labels": {OWNER: OWNER_VALUE},
                    },
                    "data": self.identity_state(),
                }
            )
        else:
            # Another installer may have created or changed state since preflight.
            require(
                state["metadata"].get("labels", {}).get(OWNER) == OWNER_VALUE,
                f"ConfigMap {INSTALL_STATE} already exists outside this installer",
            )
            self.check_identity(state)
            missing = {
                key: value
                for key, value in self.identity_state().items()
                if key not in state.get("data", {})
            }
            if missing:
                self.credentials(check_only=True)
                version = state["metadata"].get("resourceVersion")
                require(
                    version, "Installer state has no resourceVersion; rerun preflight"
                )
                # Preserve recorded fields and reject concurrent writes rather
                # than overwriting another installation's identity.
                self.run(
                    [
                        *self.kube,
                        "-n",
                        self.ns,
                        "patch",
                        "configmap",
                        INSTALL_STATE,
                        "--type=merge",
                        "-p",
                        json.dumps(
                            {
                                "metadata": {"resourceVersion": version},
                                "data": missing,
                            }
                        ),
                    ]
                )
        # Publish our reservation before scanning again: overlapping preflights
        # in other namespaces must not both activate the same OIDC identity.
        # Keep reservations on failure so a conflicting installer cannot proceed.
        self.check_identity(self.get("configmap", INSTALL_STATE))
        self.credentials()
        for index, release in enumerate(RELEASES):
            print(f"Installing {release} in {self.ns}...", flush=True)
            self.run(
                self.helm_args(
                    "upgrade",
                    "--install",
                    release,
                    str(charts[index]),
                    "--namespace",
                    self.ns,
                    "--values",
                    str(values[index]),
                    "--reset-values",
                    "--wait",
                    "--wait-for-jobs",
                    "--timeout",
                    self.timeout,
                    "--history-max",
                    "5",
                )
            )
            if index == 0:
                ca = self.get("secret", "openshell-client-tls")
                require(
                    ca and ca.get("data", {}).get("ca.crt"),
                    "Gateway did not create its CA Secret",
                )
            self.wait_for_route(release)
        print(f"Dashboard: https://{self.config['dashboard']['host']}")
        print(f"Gateway: https://{self.config['gateway']['host']}")
        print(
            "Register the gateway with the CLI using its CA, then log in through customer OIDC."
        )
        print(
            "An administrator must add users to workspaces. Verify login, denied access, sandbox creation and terminals."
        )

    def installed_releases(self):
        # Same explicit status flags as check_releases minus historical records:
        # a release stuck pending or uninstalling must still be discovered and
        # removed, or its TLS secrets would be deleted under a live gateway.
        releases = json.loads(
            self.run(
                self.helm_args(
                    "list",
                    "--namespace",
                    self.ns,
                    "--output",
                    "json",
                    "--deployed",
                    "--failed",
                    "--pending",
                    "--uninstalling",
                )
            )
        )
        return sorted(
            {release["name"] for release in releases if release["name"] in RELEASES}
        )

    def owned_credentials(self, state):
        """Return (delete OIDC secret, delete OIDC CA) from config and state."""
        oidc = self.config.get("oidc", {})
        data = (state or {}).get("data", {})
        secret = self.get("secret", OIDC_SECRET)
        ca = self.get("configmap", OIDC_CA)
        secret_label = (secret or {}).get("metadata", {}).get("labels", {}).get(OWNER)
        ca_label = (ca or {}).get("metadata", {}).get("labels", {}).get(OWNER)

        external_secret = oidc.get("client_secret_secret")
        if external_secret == OIDC_SECRET:
            # An external reference to the fixed name always wins: preserve it.
            owned_secret = False
        else:
            # A different external Secret does not change who owns OIDC_SECRET;
            # per-object label and recorded state still prove that.
            owned_secret = (
                secret_label == OWNER_VALUE
                or data.get("oidc-secret-source") == "installer"
            )

        external_ca = oidc.get("ca_configmap")
        # Any external CA reference wins, even when it reuses the fixed name.
        owned_ca = (
            False
            if external_ca
            else (ca_label == OWNER_VALUE or data.get("oidc-ca-source") == "installer")
        )
        return owned_secret, owned_ca

    def uninstall(self, assume_yes=False):
        namespace = self.get("namespace", self.ns, False)
        if not namespace:
            print(f"Namespace {self.ns} does not exist; nothing to uninstall.")
            return
        # Ownership gate before anything destructive: uninstall --namespace is
        # the most dangerous combination in the tool.
        state = self.get("configmap", INSTALL_STATE)
        metadata = namespace.get("metadata", {})
        require(
            (state or {}).get("metadata", {}).get("labels", {}).get(OWNER)
            == OWNER_VALUE
            or metadata.get("labels", {}).get(OWNER) == OWNER_VALUE
            or GATEWAY_HOST in metadata.get("annotations", {}),
            f"Namespace {self.ns} was not installed by this installer; "
            "refusing to uninstall it",
        )
        releases = self.installed_releases()
        owned_secret, owned_ca = self.owned_credentials(state)
        oidc = self.config.get("oidc", {})
        external_secret = oidc.get("client_secret_secret")
        external_ca = oidc.get("ca_configmap")
        present = {
            ("secret", name): self.get("secret", name)
            for name in (*TLS_SECRETS, OIDC_SECRET, external_secret or OIDC_SECRET)
        }
        present.update(
            {
                ("configmap", name): self.get("configmap", name)
                for name in (OIDC_CA, external_ca or OIDC_CA)
            }
        )
        planned = [f"Helm release {name}" for name in releases]
        for secret in TLS_SECRETS:
            if present.get(("secret", secret)):
                planned.append(f"Secret {secret}")
        if present.get(("secret", OIDC_SECRET)):
            if owned_secret:
                planned.append(f"Secret {OIDC_SECRET}")
            else:
                planned.append(f"Secret {OIDC_SECRET} (external, will be kept)")
        if present.get(("configmap", OIDC_CA)):
            if owned_ca:
                planned.append(f"ConfigMap {OIDC_CA}")
            else:
                planned.append(f"ConfigMap {OIDC_CA} (external, will be kept)")
        if (
            external_secret
            and external_secret != OIDC_SECRET
            and (present.get(("secret", external_secret)))
        ):
            planned.append(f"Secret {external_secret} (external, will be kept)")
        if (
            external_ca
            and external_ca != OIDC_CA
            and (present.get(("configmap", external_ca)))
        ):
            planned.append(f"ConfigMap {external_ca} (external, will be kept)")
        if state:
            planned.append(f"ConfigMap {INSTALL_STATE}")
        print(f"Removing from namespace {self.ns}:")
        for item in planned:
            print(f"  {item}")
        print("The namespace itself is kept.")
        if not assume_yes:
            require(
                sys.stdin.isatty(),
                "uninstall needs --yes when stdin is not a terminal",
            )
            try:
                answer = input(f"Type the namespace name '{self.ns}' to confirm: ")
            except EOFError:
                answer = ""
            require(
                answer.strip() == self.ns,
                "Confirmation did not match the namespace name; nothing was removed",
            )
        failures = []

        def retained_now(items):
            # Report what is still in the cluster, not what was planned.
            remaining = []
            for item in items:
                parts = item.split()
                if parts[0] in ("Secret", "ConfigMap") and self.get(
                    parts[0].lower(), parts[1]
                ):
                    remaining.append(item)
            return remaining

        def attempt(description, function):
            try:
                function()
                return True
            except (ValueError, RuntimeError, OSError) as error:
                failures.append(f"{description}: {error}")
                print(f"Warning: {description} failed: {error}")
                return False

        def remove_object(kind, name, note_when_absent=False):
            # The existence check lives inside the attempted closure so a
            # transient read failure is accumulated, not fatal mid-teardown.
            def function():
                if not self.get(kind, name):
                    if note_when_absent:
                        print(f"Note: {kind} {name} is already absent")
                    return
                self.run([*self.kube, "-n", self.ns, "delete", kind, name])

            return function

        # Dashboard first: it depends on the gateway's TLS material and the
        # OIDC Secret. Discover releases from helm list, not the config, so a
        # config edited since install cannot orphan a release.
        dashboard_removed = True
        if "openshell-dashboard" in releases:
            dashboard_removed = attempt(
                "uninstall Helm release openshell-dashboard",
                lambda: self.run(
                    self.helm_args(
                        "uninstall",
                        "openshell-dashboard",
                        "--namespace",
                        self.ns,
                        "--timeout",
                        self.timeout,
                    )
                ),
            )
        retained = []
        if not dashboard_removed:
            # The surviving dashboard still needs the gateway, the OIDC Secret,
            # and the CA ConfigMap; the state remains proof and reservation.
            retained = retained_now(planned)
            if "openshell" in releases:
                retained.insert(0, "Helm release openshell")
            self.report_retained(
                retained,
                "the surviving dashboard still needs them",
            )
            raise ValueError("uninstall incomplete:\n  " + "\n  ".join(failures))
        gateway_removed = True
        if "openshell" in releases:
            gateway_removed = attempt(
                "uninstall Helm release openshell",
                lambda: self.run(
                    self.helm_args(
                        "uninstall",
                        "openshell",
                        "--namespace",
                        self.ns,
                        "--timeout",
                        self.timeout,
                    )
                ),
            )
        if not gateway_removed:
            # The surviving gateway still uses its TLS material, and the
            # identity stays reserved for the rerun.
            retained = retained_now(planned)
            retained.insert(0, "Helm release openshell")
            self.report_retained(
                retained,
                "the surviving gateway still needs them",
            )
            raise ValueError("uninstall incomplete:\n  " + "\n  ".join(failures))
        # The certgen hook creates these at runtime, outside the release
        # manifest and without Helm labels or annotations: helm uninstall never
        # removes them, and leaving them behind blocks reinstall. Their names are
        # installer-fixed, so deletion rides on the ownership gate above rather
        # than any per-object ownership check.
        for secret in TLS_SECRETS:
            attempt(
                f"remove Secret {secret}",
                remove_object("secret", secret, note_when_absent=True),
            )
        # Installer-owned credentials only. Administrator-provided objects are
        # never deleted, modified, or relabelled, even under a fixed name.
        if owned_secret:
            attempt(
                f"remove Secret {OIDC_SECRET}",
                remove_object("secret", OIDC_SECRET),
            )
        else:
            print(f"Note: Secret {OIDC_SECRET} is external; keeping it")
        if owned_ca:
            attempt(
                f"remove ConfigMap {OIDC_CA}",
                remove_object("configmap", OIDC_CA),
            )
        else:
            print(f"Note: ConfigMap {OIDC_CA} is external or absent; keeping it")
        # Cluster-scoped RBAC belongs to the Helm releases; never delete it by
        # label, because the label is namespace-agnostic and would match other
        # installations. Just report survivors by name. Use one comma-separated
        # resource argument: kubectl parses two space-separated resources as a
        # type plus a name, which it rejects when a selector is present.
        try:
            survivors = json.loads(
                self.run(
                    [
                        *self.kube,
                        "get",
                        "clusterroles,clusterrolebindings.rbac.authorization.k8s.io",
                        "-l",
                        "app.kubernetes.io/instance in (openshell,openshell-dashboard)",
                        "-o",
                        "json",
                    ]
                )
            )
            names = sorted(
                {item["metadata"]["name"] for item in survivors.get("items", [])}
            )
            if names:
                print(f"Note: cluster-scoped RBAC left in place: {', '.join(names)}")
        except (ValueError, RuntimeError, OSError):
            print("Note: could not check for leftover cluster-scoped RBAC")
        # Last: the ConfigMap is both the OIDC reservation and the ownership
        # proof. Any earlier failure preserves it so a rerun passes the gate and
        # finishes the job; deleting it early would free the identity while a
        # release still runs and strand the leftovers.
        if not failures and state:
            attempt(
                f"remove ConfigMap {INSTALL_STATE}",
                remove_object("configmap", INSTALL_STATE),
            )
        if failures:
            self.report_retained(
                retained_now(planned), "an earlier teardown step failed"
            )
            raise ValueError("uninstall incomplete:\n  " + "\n  ".join(failures))
        print(f"Uninstalled from namespace {self.ns}; the namespace was kept.")

    @staticmethod
    def report_retained(retained, reason):
        if not retained:
            return
        print(f"Retained resources ({reason}):")
        for item in retained:
            print(f"  {item}")


def render_charts(installer, config, root, chart_dirs):
    charts, files = [], []
    for index, (name, values, local) in enumerate(
        zip(
            ("gateway", "dashboard"),
            chart_values(config),
            chart_dirs,
            strict=True,
        )
    ):
        if local:
            chart = local.resolve()
            require((chart / "Chart.yaml").is_file(), f"Missing chart: {chart}")
            # Source charts carry 0.0.0; render with the requested release image tag.
            values.setdefault("global", {})["image"] = {"tag": config[name]["version"]}
        else:
            destination = root / name
            destination.mkdir()
            installer.run(
                [
                    "helm",
                    "pull",
                    config[name]["chart"],
                    "--version",
                    config[name]["version"],
                    "--destination",
                    str(destination),
                ]
            )
            packages = list(destination.glob("*.tgz"))
            require(len(packages) == 1, f"Expected one downloaded {name} chart")
            chart = packages[0]
        value_file = root / f"{name}.json"
        value_file.write_text(json.dumps(values))
        release = RELEASES[index]
        installer.run(
            [
                "helm",
                "template",
                release,
                str(chart),
                "--namespace",
                config["namespace"],
                "--values",
                str(value_file),
                "--api-versions",
                "route.openshift.io/v1",
                "--api-versions",
                "agents.x-k8s.io/v1beta1",
            ]
        )
        charts.append(chart)
        files.append(value_file)
    print(
        "Both charts rendered successfully. This does not verify cluster or API compatibility.",
        flush=True,
    )
    return charts, files


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    config_parent = argparse.ArgumentParser(add_help=False)
    config_parent.add_argument(
        "--config",
        type=Path,
        default=Path("config.json"),
        help="Configuration file (default: ./config.json)",
    )
    cluster_parent = argparse.ArgumentParser(add_help=False)
    cluster_parent.add_argument(
        "--context",
        help="kubectl context (default: the current context)",
    )
    cluster_parent.add_argument(
        "--namespace",
        help="Namespace; overrides config.namespace",
    )
    cluster_parent.add_argument(
        "--timeout",
        default="10m",
        help="Helm/controller wait timeout (default: 10m)",
    )

    init = subparsers.add_parser(
        "init", help="Scaffold config.json and override files in the current directory"
    )
    init.add_argument(
        "--from-config",
        type=Path,
        help="Copy shared OIDC settings from an existing deployment config",
    )
    init.add_argument(
        "--oidc-issuer",
        help="Shared OIDC issuer URL (the iss claim and discovery base)",
    )
    init.add_argument(
        "--oidc-ca-file",
        help="Inline shared CA; the installer creates and owns openshell-oidc-ca",
    )
    init.add_argument(
        "--oidc-ca-configmap",
        help="Existing administrator-managed CA ConfigMap; never owned",
    )
    init.add_argument(
        "--rhoai-version",
        help="Supported RHOAI release; pins charts and product images",
    )
    init.add_argument(
        "--openshell-version",
        help="Exact gateway chart version (advanced, untested pair)",
    )
    init.add_argument(
        "--openshell-dashboard-version",
        help="Exact dashboard chart version (advanced, untested pair)",
    )
    init.add_argument("--namespace", help="Target namespace")
    init.add_argument(
        "--cluster-app-domain",
        help="Bare apps domain; derives namespace-qualified Route hosts",
    )
    init.add_argument("--gateway-host", help="Exact gateway Route host")
    init.add_argument("--dashboard-host", help="Exact dashboard Route host")
    init.add_argument("--oidc-client-id", help="Explicit browser client ID")
    init.add_argument("--oidc-audience", help="Explicit API audience")
    init.add_argument(
        "--oidc-client-secret-secret",
        help="Reference an existing Secret; omits client_secret_file",
    )

    validate = subparsers.add_parser(
        "validate",
        parents=[config_parent],
        help="Validate config and render charts without cluster access",
    )
    validate.add_argument(
        "--namespace", help="Render namespace; overrides config.namespace"
    )
    validate.add_argument(
        "--gateway-chart-directory",
        type=Path,
        help="Local source gateway chart",
    )
    validate.add_argument(
        "--dashboard-chart-directory",
        type=Path,
        help="Local source dashboard chart",
    )

    subparsers.add_parser(
        "preflight",
        parents=[config_parent, cluster_parent],
        help="Validate, render charts, and check cluster prerequisites",
    )
    subparsers.add_parser(
        "install",
        parents=[config_parent, cluster_parent],
        help="Install the gateway and dashboard",
    )
    uninstall = subparsers.add_parser(
        "uninstall",
        parents=[cluster_parent],
        help="Remove installer-created resources; the namespace is kept",
    )
    uninstall.add_argument(
        "--config",
        type=Path,
        help="Configuration file (optional; ./config.json is used when present)",
    )
    uninstall.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt"
    )

    return parser


def load_config_file(path, namespace=None):
    resolved = path.resolve()
    if not resolved.is_file():
        if path == Path("config.json"):
            raise ValueError(
                "No config.json in this directory; run init first or pass --config"
            )
        raise ValueError(f"Configuration file not found: {path}")
    print(f"Config file: {resolved}")
    return load_config(resolved, namespace)


def resolve_profile(value):
    key = value.strip().lower()
    for canonical, profile in RHOAI_PROFILES.items():
        if key == canonical.lower() or key in (
            alias.lower() for alias in profile.get("aliases", ())
        ):
            return canonical, profile
    supported = ", ".join(sorted(RHOAI_PROFILES))
    raise ValueError(
        f"Unsupported RHOAI version {value}; supported: {supported}. "
        "For an untested chart pair, pass --openshell-version and "
        "--openshell-dashboard-version."
    )


def apply_profile(config, profile):
    tag = profile["image_tag"]
    config["gateway"]["chart"] = profile["gateway_chart"]
    config["gateway"]["version"] = profile["gateway_version"]
    config["dashboard"]["chart"] = profile["dashboard_chart"]
    config["dashboard"]["version"] = profile["dashboard_version"]

    def product(repository):
        return {
            "registry": REDHAT_REGISTRY,
            "repository": f"rhoai/{repository}",
            "tag": tag,
        }

    config["gateway"]["images"] = {
        "gateway": product("odh-openshell-gateway-rhel9"),
        "supervisor": product("odh-openshell-supervisor-rhel9"),
        "sandboxRuntime": product("odh-openshell-sandbox-rhel9"),
        "sandbox": {
            "repository": f"{REDHAT_REGISTRY}/rhoai/odh-openshell-sandbox-rhel9",
            "tag": tag,
        },
    }
    config["dashboard"]["images"] = {
        "dashboard": product("odh-openshell-dashboard-rhel9"),
        "authProxy": copy.deepcopy(AUTH_PROXY_IMAGE),
    }


def seed_shared_oidc(config, source_path):
    source = json.loads(source_path.read_text())
    require(isinstance(source, dict), "--from-config must be a JSON object")
    oidc = source.get("oidc")
    validate_shared_oidc(oidc)
    copied = {
        key: copy.deepcopy(oidc[key]) for key in SHARED_OIDC_FIELDS if key in oidc
    }
    if copied.get("ca_file"):
        # Resolve relative to the source config's directory so the seeded file
        # stays valid from the new working directory.
        copied["ca_file"] = str((source_path.parent / copied["ca_file"]).resolve())
    config["oidc"].update(copied)
    require(
        not (config["oidc"].get("ca_file") and config["oidc"].get("ca_configmap")),
        "The --from-config source sets both oidc.ca_file and oidc.ca_configmap",
    )


def init_command(args):
    # init writes the client secret beside config.json; keep it owner-only.
    os.umask(0o077)
    # --from-config is an input template for the shared half, so it cannot be
    # combined with the inline shared flags it would overwrite.
    require(
        args.from_config is None
        or not any(
            value is not None
            for value in (
                args.oidc_issuer,
                args.oidc_ca_file,
                args.oidc_ca_configmap,
            )
        ),
        "--from-config cannot be combined with --oidc-issuer, --oidc-ca-file, "
        "or --oidc-ca-configmap",
    )
    require(
        not (args.oidc_ca_file and args.oidc_ca_configmap),
        "--oidc-ca-file and --oidc-ca-configmap are mutually exclusive",
    )
    require(
        not (
            args.rhoai_version
            and (args.openshell_version or args.openshell_dashboard_version)
        ),
        "--rhoai-version cannot be combined with --openshell-version or "
        "--openshell-dashboard-version",
    )
    config = copy.deepcopy(TEMPLATE_CONFIG)
    changed = False
    if args.from_config is not None:
        require(
            args.from_config.is_file(),
            f"--from-config file not found: {args.from_config}",
        )
        seed_shared_oidc(config, args.from_config)
        changed = True
    if args.namespace is not None:
        validate_namespace(args.namespace)
        config["namespace"] = args.namespace
        config["oidc"]["client_id"] = f"openshell-dashboard-{args.namespace}"
        config["oidc"]["audience"] = f"openshell-api-{args.namespace}"
        changed = True
    if args.cluster_app_domain is not None:
        domain = args.cluster_app_domain
        require(
            valid_dns_name(domain),
            "--cluster-app-domain must be a bare lowercase DNS domain, "
            "without a scheme, port, path, wildcard, or trailing dot",
        )
        for name in ("gateway", "dashboard"):
            label = f"{name}-{config['namespace']}"
            require(
                len(label) <= 63,
                f"Derived host label {label} exceeds 63 characters; "
                f"pass --{name}-host explicitly",
            )
            config[name]["host"] = f"{label}.{domain}"
        changed = True
    for name in ("gateway", "dashboard"):
        value = getattr(args, f"{name}_host")
        if value is not None:
            require(
                valid_dns_name(value),
                f"--{name}-host must be a bare lowercase DNS name",
            )
            config[name]["host"] = value
            changed = True
    require(
        config["gateway"]["host"] != config["dashboard"]["host"],
        "Gateway and dashboard need distinct hosts",
    )
    if args.rhoai_version is not None:
        canonical, profile = resolve_profile(args.rhoai_version)
        apply_profile(config, profile)
        changed = True
        if profile.get("prerelease"):
            print(
                f"Notice: RHOAI {canonical} is a pre-release build; use it for "
                "evaluation only."
            )
        if not profile.get("images_qualified"):
            print(
                f"Warning: RHOAI {canonical} product images have not been validated "
                "against the pinned charts yet."
            )
        print(
            f"Pinned RHOAI {canonical}: gateway chart "
            f"{profile['gateway_version']}, dashboard chart "
            f"{profile['dashboard_version']}, images {profile['image_tag']}."
        )
    for flag, name in (
        ("openshell_version", "gateway"),
        ("openshell_dashboard_version", "dashboard"),
    ):
        value = getattr(args, flag)
        if value is not None:
            require(
                VERSION.fullmatch(value) and value != "0.0.0",
                f"--{flag.replace('_', '-')} must be an exact published SemVer",
            )
            config[name]["version"] = value
            changed = True
    flags = (
        ("oidc_issuer", "issuer"),
        ("oidc_ca_file", "ca_file"),
        ("oidc_ca_configmap", "ca_configmap"),
        ("oidc_client_id", "client_id"),
        ("oidc_audience", "audience"),
    )
    for flag, key in flags:
        value = getattr(args, flag)
        if value is not None:
            config["oidc"][key] = value
            changed = True
    if args.oidc_ca_file is not None:
        config["oidc"].pop("ca_configmap", None)
    if args.oidc_ca_configmap is not None:
        config["oidc"].pop("ca_file", None)
    if args.oidc_client_secret_secret is not None:
        config["oidc"]["client_secret_secret"] = args.oidc_client_secret_secret
        config["oidc"].pop("client_secret_file", None)
        changed = True
    if any(
        getattr(args, flag) is not None
        for flag in (
            "oidc_issuer",
            "oidc_ca_file",
            "oidc_ca_configmap",
            "oidc_client_id",
            "oidc_audience",
            "oidc_client_secret_secret",
        )
    ):
        # Validate shared identity inputs even while a version placeholder keeps
        # full config validation from running.
        validate_shared_oidc(config["oidc"])
        require(
            config["oidc"]["client_id"] != config["oidc"]["audience"],
            "Browser client ID and gateway audience must be distinct",
        )
        for key in ("client_secret_secret", "ca_configmap"):
            if config["oidc"].get(key):
                require(
                    NAME.fullmatch(config["oidc"][key]) is not None,
                    f"Invalid oidc.{key}",
                )
    versions_ready = all(
        VERSION.fullmatch(config[name]["version"])
        and config[name]["version"] != "0.0.0"
        for name in ("gateway", "dashboard")
    )
    if versions_ready:
        # The override files do not exist yet; everything else must validate.
        trial = copy.deepcopy(config)
        for name in ("gateway", "dashboard"):
            trial[name]["helm_overrides"] = []
        validate_config(trial, Path.cwd(), require_files=False)
    config_text = json.dumps(config, indent=2) + "\n" if changed else CONFIG_TEMPLATE
    targets = {
        "config.json": config_text,
        "gateway-overrides.yaml": GATEWAY_OVERRIDES_TEMPLATE,
        "dashboard-overrides.yaml": DASHBOARD_OVERRIDES_TEMPLATE,
    }
    directory = Path.cwd()
    for name in targets:
        require(
            not (directory / name).exists(),
            f"{directory / name} already exists; refusing to overwrite",
        )
    for name, text in targets.items():
        # open("x") fails atomically if the file appeared since the check.
        with (directory / name).open("x") as target:
            target.write(text)
    if args.rhoai_version is None:
        print(
            "Notice: no --rhoai-version selected, so this config deploys upstream "
            "chart images, not the RHOAI product images. Use --rhoai-version 3.6 "
            "for RHOAI 3.6."
        )
    print(f"Created {', '.join(targets)} in {directory}")
    print(
        "Next: edit config.json (namespace, hosts, OIDC), then run ./install.py validate."
    )


def validate_command(args):
    config = load_config_file(args.config, args.namespace)
    installer = Installer(config, "10m")
    installer.audit_identity()
    with tempfile.TemporaryDirectory(prefix="openshell-install-") as temporary:
        render_charts(
            installer,
            config,
            Path(temporary),
            (args.gateway_chart_directory, args.dashboard_chart_directory),
        )


def preflight_command(args):
    config = load_config_file(args.config, args.namespace)
    installer = Installer(config, args.timeout, args.context)
    installer.audit_context()
    installer.audit_identity()
    with tempfile.TemporaryDirectory(prefix="openshell-install-") as temporary:
        render_charts(installer, config, Path(temporary), (None, None))
    installer.preflight()
    if installer.warnings:
        print(
            "\nPreflight passed with warnings; no cluster resources were changed. "
            "Review the warnings above before installing."
        )
    else:
        print("\nPreflight passed; no cluster resources were changed.")
    print("Next: ./install.py install")


def install_command(args):
    config = load_config_file(args.config, args.namespace)
    installer = Installer(config, args.timeout, args.context)
    installer.audit_context()
    installer.audit_identity()
    with tempfile.TemporaryDirectory(prefix="openshell-install-") as temporary:
        charts, files = render_charts(installer, config, Path(temporary), (None, None))
        installer.install(charts, files)


def uninstall_command(args):
    # Teardown must work when the working directory is lost; ownership is
    # proven in-cluster, so only the namespace is taken from the config.
    if args.namespace:
        config = {"namespace": args.namespace}
    elif args.config is not None:
        config = load_config_file(args.config)
    elif Path("config.json").is_file():
        config = load_config_file(Path("config.json"))
    else:
        raise ValueError(
            "Cannot determine the namespace: pass --namespace or --config, "
            "or run beside a config.json"
        )
    installer = Installer(config, args.timeout, args.context)
    installer.audit_context()
    installer.uninstall(assume_yes=args.yes)


COMMANDS = {
    "init": init_command,
    "validate": validate_command,
    "preflight": preflight_command,
    "install": install_command,
    "uninstall": uninstall_command,
}


def main():
    args = build_parser().parse_args()
    for tool in TOOLS[args.command]:
        require(shutil.which(tool), f"Required tool not found: {tool}")
    if hasattr(args, "timeout"):
        require(
            re.fullmatch(r"[1-9][0-9]*[smh]", args.timeout),
            "Timeout must be a positive duration such as 10m",
        )
    COMMANDS[args.command](args)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
        command = next(
            (argument for argument in sys.argv[1:] if not argument.startswith("-")),
            None,
        )
        label = {
            "init": "Initialization failed",
            "validate": "Validation failed",
            "preflight": "Preflight failed",
            "install": "Installation failed",
            "uninstall": "Uninstall failed",
        }.get(command, "Failed")
        detail = ""
        if command == "install":
            detail = "\nCluster resources were not rolled back or deleted."
        elif command == "preflight":
            detail = "\nNo cluster resources were changed."
        elif command == "uninstall":
            detail = "\nInspect the retained resources above and rerun uninstall."
        print(f"{label}: {error}{detail}", file=sys.stderr)
        sys.exit(1)
