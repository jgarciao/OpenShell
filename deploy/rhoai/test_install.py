# SPDX-FileCopyrightText: Copyright (c) 2026 OpenShell contributors
# SPDX-License-Identifier: Apache-2.0
# /// script
# requires-python = ">=3.11"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Behavioral tests with a simulated cluster; no network or cluster access."""

import base64
import copy
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "installer", Path(__file__).with_name("install.py")
)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def configuration():
    config = json.loads(Path(__file__).with_name("config.example.json").read_text())
    config["gateway"]["version"] = "0.1.3"
    config["dashboard"]["version"] = "0.1.7"
    config["oidc"].pop("client_secret_file")
    return config


class FakeCluster(installer.Installer):
    def __init__(self, config, timeout="10m", context=None):
        super().__init__(config, timeout, context)
        self.objects = {
            ("crd", "sandboxes.agents.x-k8s.io"): {
                "spec": {"versions": [{"name": "v1beta1", "served": True}]}
            }
        }
        self.calls = []
        self.releases = []
        self.manifests = {}
        self.fail_dashboard = False
        self.fail_gateway = False
        self.fail_delete = set()
        self.current_context = "fake-context"
        self.csvs = []
        self.routes = []
        self.cluster_rbac = []
        self.storage = [
            {
                "metadata": {
                    "name": "block",
                    "annotations": {
                        "storageclass.kubernetes.io/is-default-class": "true"
                    },
                }
            }
        ]
        self.other_states = []
        self.denied_permission = None

    def run(self, args, payload=None):
        self.calls.append((args, payload))
        if args[0] == "kubectl" and "csv" in args:
            return json.dumps({"items": self.csvs})
        if args[0] == "kubectl" and "routes" in args and "--all-namespaces" in args:
            return json.dumps({"items": self.routes})
        if args[0] == "kubectl" and args[1:3] == ["config", "view"]:
            contexts = [{"name": "fake-context", "context": {"cluster": "fake"}}]
            if self.context:
                contexts.append(
                    {"name": self.context, "context": {"cluster": "context-cluster"}}
                )
            return json.dumps(
                {"current-context": self.current_context, "contexts": contexts}
            )
        if (
            args[0] == "kubectl"
            and "-n" in args
            and "get" in args
            and ("namespace", args[args.index("-n") + 1]) not in self.objects
        ):
            raise RuntimeError("namespace not found")
        if "can-i" in args:
            verb, resource = args[args.index("can-i") + 1 :][:2]
            return (
                "no"
                if self.denied_permission
                and (
                    self.denied_permission in args
                    or self.denied_permission == (verb, resource)
                )
                else "yes"
            )
        if "storageclasses" in args:
            return json.dumps({"items": self.storage})
        if "nodes" in args:
            return json.dumps({"items": []})
        if "--all-namespaces" in args:
            return json.dumps({"items": self.other_states})
        if any(arg.startswith("clusterroles") for arg in args) and "-l" in args:
            # kubectl parses a second space-separated resource as a NAME, which
            # it rejects when a selector is present. Emulate that so argv
            # regressions surface here instead of only against a real cluster.
            start = args.index("get") + 1
            resources = []
            for token in args[start:]:
                if token.startswith("-"):
                    break
                resources.append(token)
            if len(resources) > 1:
                raise RuntimeError(
                    "name cannot be provided when a selector is specified"
                )
            return json.dumps({"items": self.cluster_rbac})
        if args[0] == "kubectl" and "delete" in args:
            index = args.index("delete")
            keys = [args[index + 1], args[index + 2]]
            if any(key in self.fail_delete for key in keys):
                raise RuntimeError(f"cannot delete {keys[-1]}")
            self.objects.pop((keys[0], keys[1]), None)
            return ""
        if "patch" in args:
            state = self.objects[("configmap", installer.INSTALL_STATE)]
            patch_data = json.loads(args[args.index("-p") + 1])
            version = patch_data.get("metadata", {}).get("resourceVersion")
            if version is not None and version != state["metadata"]["resourceVersion"]:
                raise RuntimeError("Conflict: resourceVersion changed")
            state.setdefault("data", {}).update(patch_data["data"])
            state["metadata"]["resourceVersion"] = str(
                int(state["metadata"]["resourceVersion"]) + 1
            )
            return ""
        if args[:2] == ["helm", "list"]:
            offset = int(args[args.index("--offset") + 1]) if "--offset" in args else 0
            return json.dumps(self.releases[offset : offset + 256])
        if args[:2] == ["helm", "uninstall"]:
            name = args[2]
            if self.fail_dashboard and name == "openshell-dashboard":
                raise RuntimeError("dashboard uninstall failed")
            if self.fail_gateway and name == "openshell":
                raise RuntimeError("gateway uninstall failed")
            self.releases = [
                release for release in self.releases if release["name"] != name
            ]
            return ""
        if args[:2] == ["helm", "pull"]:
            destination = Path(args[args.index("--destination") + 1])
            (destination / "chart.tgz").write_bytes(b"fake")
            return ""
        if args[:2] == ["helm", "template"]:
            return ""
        if args[:3] == ["helm", "get", "manifest"]:
            return self.manifests.get(args[3], "")
        if args[:2] == ["helm", "upgrade"]:
            if self.fail_dashboard and "openshell-dashboard" in args:
                raise RuntimeError("dashboard failed")
            self.objects[("secret", "openshell-client-tls")] = {
                "data": {"ca.crt": "Y2E="}
            }
            self.objects[("route", args[3])] = {
                "status": {
                    "ingress": [
                        {
                            "host": self.config[
                                "gateway" if args[3] == "openshell" else "dashboard"
                            ]["host"],
                            "conditions": [{"type": "Admitted", "status": "True"}],
                        }
                    ]
                }
            }
            if not any(r["name"] == args[3] for r in self.releases):
                self.releases.append(
                    {
                        "name": args[3],
                        "chart": "helm-chart-0.1.3"
                        if args[3] == "openshell"
                        else "openshell-dashboard-0.1.0",
                    }
                )
        elif "get" in args and "--raw" not in args:
            start = args.index("get")
            obj = self.objects.get(tuple(args[start + 1 : start + 3]))
            return json.dumps(obj) if obj is not None else ""
        elif "create" in args:
            obj = json.loads(payload)
            key = (obj["kind"].lower(), obj["metadata"]["name"])
            if key in self.objects:
                raise RuntimeError("already exists")
            obj["metadata"]["resourceVersion"] = "1"
            self.objects[key] = obj
        return ""


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = configuration()
        # The example config wires in the override files; provide them beside
        # every generated config.json just like init does.
        for component in ("gateway", "dashboard"):
            (self.root / f"{component}-overrides.yaml").write_text(
                Path(__file__)
                .with_name(f"{component}-overrides.example.yaml")
                .read_text()
            )

    def load(self):
        path = self.root / "config.json"
        path.write_text(json.dumps(self.config))
        return installer.load_config(path)

    def cluster(self):
        secret = self.root / "client.txt"
        secret.write_text("a-secret-that-must-not-appear-in-argv\n")
        self.config["oidc"]["client_secret_file"] = str(secret)
        return FakeCluster(self.load())

    def test_rejects_unpinned_versions_and_unknown_keys(self):
        for version in (
            "latest",
            "0.0.0",
            "01.2.3",
            "1.2.3-01",
            ">=0.1.0",
            "REPLACE_ME",
        ):
            with self.subTest(version=version):
                self.config["gateway"]["version"] = version
                with self.assertRaises(ValueError):
                    self.load()
        self.config = configuration()
        self.config["oidc"]["client_secret"] = "never-inline"
        with self.assertRaisesRegex(ValueError, "Unknown"):
            self.load()

    def test_pinned_downstream_and_new_release_versions(self):
        for version in ("0.0.0-dev.e7fdd6b", "0.2.1", "1.2.3+rhaiv.5"):
            self.config["gateway"]["version"] = version
            self.assertEqual(self.load()["gateway"]["version"], version)

    def test_yaml_overlays_merge_in_order_and_preserve_managed_values(self):
        (self.root / "base.yaml").write_text(
            "resources:\n  requests:\n    cpu: 100m\n    memory: 256Mi\n"
        )
        (self.root / "site.yaml").write_text(
            "resources:\n  requests:\n    cpu: 200m\nimagePullSecrets:\n  - name: registry-creds\n"
        )
        self.config["gateway"]["helm_overrides"] = ["base.yaml", "site.yaml"]
        self.config["oidc"]["scope"] = "openid email profile offline_access"
        gateway, dashboard = installer.chart_values(self.load())
        self.assertEqual(
            gateway["resources"]["requests"], {"cpu": "200m", "memory": "256Mi"}
        )
        self.assertEqual(gateway["imagePullSecrets"], [{"name": "registry-creds"}])
        self.assertEqual(
            gateway["server"]["sandboxNamespace"], self.config["namespace"]
        )
        self.assertEqual(
            dashboard["oidc"]["scope"], "openid email profile offline_access"
        )

    def test_overlays_cannot_replace_authentication_or_namespace_contract(self):
        for name, overlay in (
            ("gateway", "server:\n  auth:\n    allowUnauthenticatedUsers: true"),
            ("gateway", "server: null"),
            ("gateway", "fullnameOverride: foreign"),
            ("dashboard", "authProxy:\n  extraArgs: [--skip-auth-regex=.*]"),
            ("dashboard", "networkPolicy:\n  enabled: false"),
            ("dashboard", "oidc:\n  existingSecret: foreign"),
        ):
            with self.subTest(overlay=overlay):
                self.config = configuration()
                (self.root / "site.yaml").write_text(overlay)
                self.config[name]["helm_overrides"] = ["site.yaml"]
                with self.assertRaises(ValueError):
                    self.load()

    def test_scope_and_system_namespace_validation(self):
        for scope in (None, [], "profile email", "openid\nemail"):
            self.config["oidc"]["scope"] = scope
            with self.assertRaises(ValueError):
                self.load()
        self.config = configuration()
        for ns in ("default", "openshift", "openshift-ingress", "kube-system"):
            self.config["namespace"] = ns
            with self.assertRaisesRegex(ValueError, "system namespace"):
                self.load()

    def test_preflight_storage_and_permissions_fail_before_mutation(self):
        for issue in ("storage", "permission", "workspace-storage"):
            cluster = self.cluster()
            if issue == "storage":
                cluster.storage = []
            elif issue == "permission":
                cluster.denied_permission = "deployments.apps"
            else:
                cluster.config["gateway"]["values"] = {
                    "server": {"workspaceStorageClass": "missing"}
                }
            with self.subTest(issue=issue), self.assertRaises(ValueError):
                cluster.install([], [])
            self.assertFalse(
                any(
                    "create" in args and "can-i" not in args
                    for args, _ in cluster.calls
                )
            )
            self.assertFalse(
                any(args[:2] == ["helm", "upgrade"] for args, _ in cluster.calls)
            )

    def test_cross_namespace_identity_reuse_is_rejected(self):
        for field in ("oidc-audience", "oidc-client-id"):
            cluster = self.cluster()
            other = {
                **cluster.identity_state(),
                "oidc-audience": "other-api",
                "oidc-client-id": "other-browser",
            }
            other[field] = cluster.identity_state()[field]
            cluster.other_states = [
                {
                    "metadata": {"name": installer.INSTALL_STATE, "namespace": "other"},
                    "data": other,
                }
            ]
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(ValueError, "distinct per-gateway"),
            ):
                cluster.preflight()
        cluster.other_states[0]["data"]["oidc-issuer"] = (
            "https://another-issuer.example"
        )
        cluster.preflight()

    def test_identity_change_requires_migration(self):
        cluster = self.cluster()
        cluster.install(["g.tgz", "d.tgz"], ["g.json", "d.json"])
        cluster.config["oidc"]["audience"] = "different-api"
        with self.assertRaisesRegex(ValueError, "identity.*changed"):
            cluster.preflight()

    def test_overlapping_namespace_installs_cannot_both_activate(self):
        for overlap in (False, True):
            with self.subTest(overlap=overlap):
                first = self.cluster()
                config = copy.deepcopy(first.config)
                config["namespace"] = "other-team"
                second = FakeCluster(config)
                reservations = []
                first.other_states = second.other_states = reservations
                # Both runs pass preflight before either publishes its identity.
                first.preflight()
                second.preflight()
                first_create, second_create = first.create, second.create

                def publish_second(resource, create=second_create, states=reservations):
                    create(resource)
                    if resource["metadata"]["name"] == installer.INSTALL_STATE:
                        states.append(copy.deepcopy(resource))

                def publish_first(
                    resource,
                    create=first_create,
                    states=reservations,
                    overlap=overlap,
                    other=second,
                ):
                    create(resource)
                    if resource["metadata"]["name"] == installer.INSTALL_STATE:
                        states.append(copy.deepcopy(resource))
                        if overlap:
                            with self.assertRaisesRegex(
                                ValueError, "distinct per-gateway"
                            ):
                                other.install(["g", "d"], ["g", "d"])

                with (
                    patch.object(first, "preflight", return_value=None),
                    patch.object(second, "preflight", return_value=None),
                    patch.object(first, "create", side_effect=publish_first),
                    patch.object(second, "create", side_effect=publish_second),
                ):
                    if overlap:
                        with self.assertRaisesRegex(ValueError, "distinct per-gateway"):
                            first.install(["g", "d"], ["g", "d"])
                    else:
                        first.install(["g", "d"], ["g", "d"])
                        with self.assertRaisesRegex(ValueError, "distinct per-gateway"):
                            second.install(["g", "d"], ["g", "d"])
                self.assertEqual(len(reservations), 2)
                for cluster in (first, second) if overlap else (second,):
                    self.assertFalse(
                        any(
                            args[:2] == ["helm", "upgrade"] for args, _ in cluster.calls
                        )
                    )
                    self.assertIn(
                        ("configmap", installer.INSTALL_STATE), cluster.objects
                    )

    def test_state_created_after_preflight_preserves_successful_installation(self):
        loser = self.cluster()
        loser.objects[("namespace", loser.ns)] = {"metadata": {}}
        config = copy.deepcopy(loser.config)
        config["oidc"]["audience"] = "winning-audience"
        winner = FakeCluster(config)
        winner.objects = loser.objects
        preflight = loser.preflight
        successful = None

        def competing_install():
            nonlocal successful
            namespace = preflight()
            winner.install(["g", "d"], ["g", "d"])
            successful = copy.deepcopy(winner.objects)
            return namespace

        with (
            patch.object(loser, "preflight", side_effect=competing_install),
            self.assertRaisesRegex(ValueError, "identity.*changed"),
        ):
            loser.install(["g", "d"], ["g", "d"])
        self.assertEqual(winner.objects, successful)
        self.assertFalse(
            any(args[:2] == ["helm", "upgrade"] for args, _ in loser.calls)
        )
        winner.preflight()

    def legacy_state(self, cluster):
        cluster.objects[("namespace", cluster.ns)] = {"metadata": {}}
        state = {
            "metadata": {
                "name": installer.INSTALL_STATE,
                "namespace": cluster.ns,
                "resourceVersion": "1",
                "labels": {installer.OWNER: installer.OWNER_VALUE, "team": "research"},
            },
            "data": {
                "gateway-host": cluster.config["gateway"]["host"],
                "custom-field": "preserve-me",
            },
        }
        cluster.objects[("configmap", installer.INSTALL_STATE)] = state
        return state

    def test_legacy_state_populates_only_missing_identity_fields(self):
        cluster = self.cluster()
        state = self.legacy_state(cluster)
        state["data"]["oidc-issuer"] = cluster.config["oidc"]["issuer"]
        cluster.install(["g", "d"], ["g", "d"])
        self.assertEqual(
            state["data"], {**cluster.identity_state(), "custom-field": "preserve-me"}
        )
        self.assertEqual(state["metadata"]["labels"]["team"], "research")
        patches = [
            json.loads(args[args.index("-p") + 1])
            for args, _ in cluster.calls
            if "patch" in args and "can-i" not in args
        ]
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["metadata"], {"resourceVersion": "1"})
        self.assertEqual(
            set(patches[0]["data"]),
            {"oidc-audience", "oidc-client-id", "oidc-secret-source", "oidc-ca-source"},
        )
        cluster.calls.clear()
        cluster.install(["g", "d"], ["g", "d"])
        self.assertFalse(
            any("patch" in args and "can-i" not in args for args, _ in cluster.calls)
        )

    def test_concurrent_legacy_state_update_is_not_overwritten(self):
        cluster = self.cluster()
        state = self.legacy_state(cluster)
        run = cluster.run
        concurrent = None

        def change_before_patch(args, payload=None):
            nonlocal concurrent
            if "patch" in args and "can-i" not in args:
                state["metadata"]["resourceVersion"] = "2"
                state["data"]["oidc-audience"] = "other-audience"
                concurrent = copy.deepcopy(state)
            return run(args, payload)

        with (
            patch.object(cluster, "run", side_effect=change_before_patch),
            self.assertRaisesRegex(RuntimeError, "Conflict"),
        ):
            cluster.install(["g", "d"], ["g", "d"])
        self.assertEqual(state, concurrent)
        self.assertNotIn(("secret", installer.OIDC_SECRET), cluster.objects)
        self.assertFalse(
            any(args[:2] == ["helm", "upgrade"] for args, _ in cluster.calls)
        )

    def test_state_ownership_change_after_preflight_is_rejected(self):
        cluster = self.cluster()
        state = self.legacy_state(cluster)
        preflight = cluster.preflight

        def change_owner():
            namespace = preflight()
            state["metadata"]["labels"][installer.OWNER] = "someone-else"
            return namespace

        before = copy.deepcopy(state["data"])
        with (
            patch.object(cluster, "preflight", side_effect=change_owner),
            self.assertRaisesRegex(ValueError, "outside this installer"),
        ):
            cluster.install(["g", "d"], ["g", "d"])
        self.assertEqual(state["data"], before)

    def test_legacy_migration_rechecks_credentials_before_writing_state(self):
        cluster = self.cluster()
        state = self.legacy_state(cluster)
        before = copy.deepcopy(state)
        preflight = cluster.preflight

        def change_credentials():
            namespace = preflight()
            cluster.objects[("secret", installer.OIDC_SECRET)] = {
                "data": {
                    "client-secret": base64.b64encode(
                        b"another-client-secret"
                    ).decode(),
                    "cookie-secret": base64.b64encode(b"cookie").decode(),
                }
            }
            return namespace

        with (
            patch.object(cluster, "preflight", side_effect=change_credentials),
            self.assertRaisesRegex(ValueError, "rotate"),
        ):
            cluster.install(["g", "d"], ["g", "d"])
        self.assertEqual(state, before)
        self.assertFalse(
            any(args[:2] == ["helm", "upgrade"] for args, _ in cluster.calls)
        )

    def test_secret_storage_permissions_are_required_before_any_mutation(self):
        for verb in ("update", "delete"):
            with self.subTest(verb=verb):
                cluster = self.cluster()
                cluster.denied_permission = (verb, "secrets")
                before = copy.deepcopy(cluster.objects)
                with self.assertRaisesRegex(ValueError, f"{verb} secrets"):
                    cluster.install(["g", "d"], ["g", "d"])
                self.assertEqual(cluster.objects, before)
                self.assertFalse(
                    any(args[:2] == ["helm", "upgrade"] for args, _ in cluster.calls)
                )

    def test_uninstalled_other_release_history_does_not_block_install(self):
        cluster = self.cluster()
        cluster.objects[("namespace", cluster.ns)] = {"metadata": {}}
        cluster.releases = [
            {
                "name": "retired-gateway",
                "chart": "helm-chart-0.1.3",
                "status": "uninstalled",
            }
        ]
        cluster.manifests["retired-gateway"] = self.gateway_manifest()
        cluster.preflight()
        self.assertFalse(
            any(args[:3] == ["helm", "get", "manifest"] for args, _ in cluster.calls)
        )
        cluster.objects[("secret", "openshell-server-tls")] = {"data": {}}
        with self.assertRaisesRegex(ValueError, "explicit migration"):
            cluster.preflight()

    def test_preflight_rejects_credential_drift_without_changing_state(self):
        cluster = self.cluster()
        cluster.install(["g.tgz", "d.tgz"], ["g.json", "d.json"])
        before = copy.deepcopy(cluster.objects)
        Path(cluster.config["oidc"]["client_secret_file"]).write_text("changed")
        cluster.calls.clear()
        with self.assertRaisesRegex(ValueError, "rotate"):
            cluster.install(["g.tgz", "d.tgz"], ["g.json", "d.json"])
        self.assertEqual(cluster.objects, before)
        self.assertFalse(
            any("patch" in args and "can-i" not in args for args, _ in cluster.calls)
        )

    def test_preflight_mode_renders_and_checks_without_installing(self):
        path = self.root / "config.json"
        path.write_text(json.dumps(self.config))

        def run(_instance, args, payload=None):
            self.assertIsNone(payload)
            if args[1:3] == ["config", "view"]:
                return json.dumps(
                    {
                        "current-context": "test-context",
                        "contexts": [
                            {"name": "test-context", "context": {"cluster": "cluster"}}
                        ],
                    }
                )
            if args[:2] == ["helm", "pull"]:
                (Path(args[args.index("--destination") + 1]) / "chart.tgz").write_bytes(
                    b"fake"
                )
            return ""

        with (
            patch("sys.argv", ["install.py", "preflight", "--config", str(path)]),
            patch.object(installer.shutil, "which", return_value="tool"),
            patch.object(installer.Installer, "run", run),
            patch.object(installer.Installer, "preflight") as preflight,
            patch.object(installer.Installer, "install") as install,
        ):
            installer.main()
        preflight.assert_called_once()
        install.assert_not_called()

    def test_rejects_invalid_oidc_and_namespace(self):
        for section, key, value in (
            ("oidc", "issuer", "http://issuer.example"),
            ("oidc", "issuer", "https://user:password@issuer.example"),
            ("oidc", "audience", configuration()["oidc"]["client_id"]),
            ("oidc", "allowed_roles", ["admin"]),
            ("gateway", "host", "gateway..example"),
        ):
            self.config = configuration()
            self.config[section][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.load()
        self.config = configuration()
        self.config["namespace"] = "openshell/another"
        with self.assertRaises(ValueError):
            self.load()

    def test_generated_connection_and_identity_contract(self):
        gateway, dashboard = installer.chart_values(self.load())
        self.assertIn(
            "openshell.openshell-team.svc.cluster.local",
            gateway["pkiInitJob"]["serverDnsNames"],
        )
        self.assertEqual(
            dashboard["gateway"]["url"],
            "grpcs://openshell.openshell-team.svc.cluster.local:8080",
        )
        self.assertEqual(
            gateway["server"]["oidc"]["issuer"], dashboard["oidc"]["issuer"]
        )
        self.assertFalse(gateway["server"]["tls"]["enableMtls"])
        self.assertIsNone(gateway["securityContext"]["runAsUser"])

    def test_existing_admin_namespace_preserves_metadata_and_targets_releases(self):
        self.config["namespace"] = "research-team"
        cluster = self.cluster()
        namespace = {
            "metadata": {
                "name": cluster.ns,
                "labels": {"team": "research"},
                "annotations": {"openshift.io/sa.scc.uid-range": "1000100000/10000"},
            },
            "status": {"phase": "Active"},
        }
        cluster.objects[("namespace", cluster.ns)] = copy.deepcopy(namespace)
        cluster.install(["gateway.tgz", "dashboard.tgz"], ["g.json", "d.json"])
        cluster.install(["gateway.tgz", "dashboard.tgz"], ["g.json", "d.json"])
        self.assertEqual(cluster.objects[("namespace", cluster.ns)], namespace)
        self.assertEqual(
            {r["name"] for r in cluster.releases}, {"openshell", "openshell-dashboard"}
        )
        for args, payload in cluster.calls:
            if args[:2] == ["helm", "upgrade"]:
                self.assertEqual(args[args.index("--namespace") + 1], "research-team")
            if "create" in args and "can-i" not in args:
                resource = json.loads(payload)
                self.assertNotEqual(resource["kind"], "Namespace")
                self.assertEqual(resource["metadata"]["namespace"], "research-team")
        gateway, dashboard = installer.chart_values(cluster.config)
        self.assertEqual(gateway["server"]["sandboxNamespace"], "research-team")
        self.assertIn(
            "openshell.research-team.svc", gateway["pkiInitJob"]["serverDnsNames"]
        )
        self.assertEqual(
            dashboard["gateway"]["url"],
            "grpcs://openshell.research-team.svc.cluster.local:8080",
        )

    def test_namespace_override_validated_and_config_namespace_optional_with_override(
        self,
    ):
        self.config.pop("namespace")
        path = self.root / "config.json"
        path.write_text(json.dumps(self.config))
        self.assertEqual(
            installer.load_config(path, "admin-project")["namespace"], "admin-project"
        )
        with self.assertRaisesRegex(ValueError, "Invalid namespace"):
            installer.load_config(path, "invalid/namespace")
        with self.assertRaisesRegex(ValueError, "namespace"):
            installer.load_config(path)

    def test_existing_gateway_or_foreign_release_rejected_before_mutation(self):
        for release in (
            {"name": "another-gateway", "chart": "helm-chart-0.1.3"},
            {"name": "openshell", "chart": "helm-chart-0.1.3"},
            {"name": "openshell-dashboard", "chart": "other-chart-1.0.0"},
        ):
            with self.subTest(release=release):
                cluster = self.cluster()
                cluster.objects[("namespace", cluster.ns)] = {"metadata": {}}
                cluster.releases = [release]
                with self.assertRaises(ValueError):
                    cluster.install([], [])
                self.assertFalse(
                    any(
                        ("create" in args and "can-i" not in args)
                        or args[:2] == ["helm", "upgrade"]
                        for args, _ in cluster.calls
                    )
                )

    @staticmethod
    def gateway_manifest(kind="StatefulSet", container=None):
        return json.dumps(
            {
                "apiVersion": "apps/v1",
                "kind": kind,
                "metadata": {"name": "renamed-gateway"},
                "spec": {
                    "replicas": 0,
                    "template": {
                        "spec": {
                            "containers": [
                                container
                                or {
                                    "name": "openshell-gateway",
                                    "image": "registry.example/custom/runtime:1.0",
                                }
                            ]
                        }
                    },
                },
            }
        )

    def test_repackaged_gateway_rejected_before_mutation(self):
        for kind, status in (("StatefulSet", "deployed"), ("Deployment", "failed")):
            with self.subTest(kind=kind, status=status):
                cluster = self.cluster()
                cluster.objects[("namespace", cluster.ns)] = {"metadata": {}}
                cluster.releases = [
                    {"name": "team-gateway", "chart": "custom-1.0.0", "status": status}
                ]
                cluster.manifests["team-gateway"] = self.gateway_manifest(kind)
                before = copy.deepcopy(cluster.objects)
                with self.assertRaisesRegex(ValueError, "one gateway per namespace"):
                    cluster.install(["g.tgz", "d.tgz"], ["g.json", "d.json"])
                self.assertEqual(cluster.objects, before)
                self.assertFalse(
                    any(args[:2] == ["helm", "upgrade"] for args, _ in cluster.calls)
                )

    def test_gateway_executable_detected_with_renamed_container(self):
        cluster = self.cluster()
        cluster.manifests["custom"] = self.gateway_manifest(
            container={
                "name": "server",
                "command": ["/usr/local/bin/openshell-gateway"],
            }
        )
        self.assertTrue(cluster.release_has_gateway("custom"))

    def test_unrelated_release_with_workload_is_preserved(self):
        cluster = self.cluster()
        cluster.objects[("namespace", cluster.ns)] = {"metadata": {}}
        cluster.releases = [{"name": "other-app", "chart": "custom-1.0.0"}]
        cluster.manifests["other-app"] = self.gateway_manifest(
            "Deployment", {"name": "web", "image": "registry.example/web:1.0"}
        )
        cluster.install(["g.tgz", "d.tgz"], ["g.json", "d.json"])
        self.assertIn("other-app", [release["name"] for release in cluster.releases])

    def test_invalid_release_manifest_stops_without_echoing_content(self):
        cluster = self.cluster()
        cluster.manifests["custom"] = "secret: [do-not-print-this"
        with self.assertRaisesRegex(ValueError, "Cannot inspect manifest") as caught:
            cluster.release_has_gateway("custom")
        self.assertNotIn("do-not-print-this", str(caught.exception))

    def test_conflict_on_later_release_page_is_detected(self):
        cluster = self.cluster()
        cluster.releases = [
            {"name": f"other-{i}", "chart": "unrelated-1.0.0"} for i in range(256)
        ]
        cluster.releases.append(
            {"name": "another-gateway", "chart": "helm-chart-0.1.3"}
        )
        with self.assertRaisesRegex(ValueError, "one gateway"):
            cluster.check_releases(managed=True)

    def test_terminating_namespace_is_rejected(self):
        cluster = self.cluster()
        cluster.objects[("namespace", cluster.ns)] = {
            "metadata": {},
            "status": {"phase": "Terminating"},
        }
        with self.assertRaisesRegex(ValueError, "terminating"):
            cluster.install([], [])

    def test_preexisting_gateway_certificate_is_not_reused(self):
        cluster = self.cluster()
        cluster.objects[("namespace", cluster.ns)] = {"metadata": {}}
        cluster.objects[("secret", "openshell-server-tls")] = {
            "data": {"tls.crt": "old"}
        }
        with self.assertRaisesRegex(ValueError, "explicit migration"):
            cluster.install([], [])

    def test_legacy_install_migrates_without_modifying_namespace(self):
        cluster = self.cluster()
        namespace = {
            "metadata": {
                "labels": {installer.OWNER: installer.OWNER_VALUE},
                "annotations": {
                    installer.GATEWAY_HOST: cluster.config["gateway"]["host"]
                },
            }
        }
        cluster.objects[("namespace", cluster.ns)] = copy.deepcopy(namespace)
        cluster.releases = [{"name": "openshell", "chart": "helm-chart-0.1.3"}]
        cluster.install(["g.tgz", "d.tgz"], ["g.json", "d.json"])
        self.assertEqual(cluster.objects[("namespace", cluster.ns)], namespace)
        self.assertEqual(
            cluster.objects[("configmap", installer.INSTALL_STATE)]["data"][
                "gateway-host"
            ],
            cluster.config["gateway"]["host"],
        )

    def test_missing_prerequisite_stops_before_mutation(self):
        cluster = self.cluster()
        cluster.objects.clear()
        with self.assertRaisesRegex(ValueError, "Agent Sandbox"):
            cluster.install([], [])
        self.assertFalse(
            any("create" in args and "can-i" not in args for args, _ in cluster.calls)
        )

    def test_install_order_context_and_secret_transport(self):
        cluster = self.cluster()
        cluster.install(["gateway.tgz", "dashboard.tgz"], ["g.json", "d.json"])
        helm = [args for args, _ in cluster.calls if args[0] == "helm"]
        self.assertEqual(
            [args[3] for args in helm], ["openshell", "openshell-dashboard"]
        )
        for args, _ in cluster.calls:
            self.assertNotIn("a-secret-that", " ".join(args))
            self.assertNotIn("--context", args)
            self.assertNotIn("--kube-context", args)
            self.assertNotIn("--atomic", args)
            if "can-i" not in args:
                self.assertNotIn("delete", args)
        data = cluster.objects[("secret", installer.OIDC_SECRET)]["data"]
        self.assertEqual(
            base64.b64decode(data["client-secret"]),
            b"a-secret-that-must-not-appear-in-argv",
        )
        cookie = base64.b64decode(data["cookie-secret"]).decode()
        self.assertEqual(len(base64.urlsafe_b64decode(cookie + "=")), 32)

    def test_rerun_preserves_secrets_without_input_file(self):
        cluster = self.cluster()
        cluster.install(["g.tgz", "d.tgz"], ["g.json", "d.json"])
        before = copy.deepcopy(cluster.objects[("secret", installer.OIDC_SECRET)])
        cluster.config["oidc"].pop("client_secret_file")
        cluster.calls.clear()
        cluster.install(["g.tgz", "d.tgz"], ["g.json", "d.json"])
        self.assertEqual(cluster.objects[("secret", installer.OIDC_SECRET)], before)
        self.assertFalse(
            any("create" in args and "can-i" not in args for args, _ in cluster.calls)
        )

    def test_first_install_requires_supplied_secret_even_with_existing_secret(self):
        for supplied in (None, "different", "matching"):
            with self.subTest(supplied=supplied):
                cluster = self.cluster()
                cluster.objects[("namespace", cluster.ns)] = {"metadata": {}}
                existing = {
                    "data": {
                        "client-secret": base64.b64encode(b"matching").decode(),
                        "cookie-secret": base64.b64encode(b"existing-cookie").decode(),
                    }
                }
                cluster.objects[("secret", installer.OIDC_SECRET)] = copy.deepcopy(
                    existing
                )
                path = Path(cluster.config["oidc"]["client_secret_file"])
                if supplied is None:
                    cluster.config["oidc"].pop("client_secret_file")
                else:
                    path.write_text(supplied)
                before = copy.deepcopy(cluster.objects)
                if supplied == "matching":
                    cluster.install(["g.tgz", "d.tgz"], ["g.json", "d.json"])
                    self.assertEqual(
                        cluster.objects[("secret", installer.OIDC_SECRET)], existing
                    )
                else:
                    expected = (
                        "required for the first install"
                        if supplied is None
                        else "rotate"
                    )
                    with self.assertRaisesRegex(ValueError, expected):
                        cluster.install(["g.tgz", "d.tgz"], ["g.json", "d.json"])
                    self.assertEqual(cluster.objects, before)

    def test_new_namespace_preflight_checks_files_without_namespaced_reads(self):
        (self.root / "issuer.pem").write_text("test CA")
        self.config["oidc"]["ca_file"] = "issuer.pem"
        cluster = self.cluster()
        before = copy.deepcopy(cluster.objects)
        self.assertIsNone(cluster.preflight())
        self.assertEqual(cluster.objects, before)
        self.assertFalse(
            any("-n" in args and "get" in args for args, _ in cluster.calls)
        )
        path = Path(cluster.config["oidc"]["client_secret_file"])
        secret = path.read_text()
        path.write_text("\n")
        with self.assertRaisesRegex(ValueError, "contains no secret"):
            cluster.preflight()
        self.assertEqual(cluster.objects, before)
        path.write_text(secret)
        cluster.install(["g.tgz", "d.tgz"], ["g.json", "d.json"])
        self.assertIn(("namespace", cluster.ns), cluster.objects)
        self.assertIn(("secret", installer.OIDC_SECRET), cluster.objects)
        self.assertIn(("configmap", installer.OIDC_CA), cluster.objects)

    def test_changed_gateway_host_requires_certificate_migration(self):
        cluster = self.cluster()
        cluster.install(["g.tgz", "d.tgz"], ["g.json", "d.json"])
        cluster.config["gateway"]["host"] = "other.apps.example.com"
        cluster.calls.clear()
        with self.assertRaisesRegex(ValueError, "certificate migration"):
            cluster.install(["g.tgz", "d.tgz"], ["g.json", "d.json"])
        self.assertFalse(
            any(
                args[0] == "helm" or ("create" in args and "can-i" not in args)
                for args, _ in cluster.calls
            )
        )

    def test_different_secret_is_not_silently_rotated(self):
        cluster = self.cluster()
        cluster.objects[("namespace", cluster.ns)] = {"metadata": {}}
        cluster.credentials()
        before = copy.deepcopy(cluster.objects)
        Path(cluster.config["oidc"]["client_secret_file"]).write_text("changed")
        with self.assertRaisesRegex(ValueError, "rotate"):
            cluster.credentials()
        self.assertEqual(cluster.objects, before)

    def test_private_ca_and_relative_path(self):
        (self.root / "issuer.pem").write_text("test CA")
        self.config["oidc"]["ca_file"] = "issuer.pem"
        cluster = self.cluster()
        cluster.objects[("namespace", cluster.ns)] = {"metadata": {}}
        cluster.credentials()
        gateway, dashboard = installer.chart_values(cluster.config)
        self.assertEqual(
            gateway["server"]["oidc"]["caConfigMapName"], installer.OIDC_CA
        )
        self.assertEqual(dashboard["oidc"]["caConfigMapName"], installer.OIDC_CA)
        self.assertEqual(
            cluster.objects[("configmap", installer.OIDC_CA)]["data"]["ca.crt"],
            "test CA",
        )

    def test_dashboard_failure_keeps_gateway_and_credentials(self):
        cluster = self.cluster()
        cluster.fail_dashboard = True
        with self.assertRaisesRegex(RuntimeError, "dashboard failed"):
            cluster.install(["g.tgz", "d.tgz"], ["g.json", "d.json"])
        self.assertIn(("namespace", cluster.ns), cluster.objects)
        self.assertIn(("secret", installer.OIDC_SECRET), cluster.objects)
        self.assertIn(("secret", "openshell-client-tls"), cluster.objects)
        self.assertFalse(
            any(
                "uninstall" in args or ("delete" in args and "can-i" not in args)
                for args, _ in cluster.calls
            )
        )

    def test_route_admission_waits_and_times_out(self):
        cluster = self.cluster()
        cluster.objects[("namespace", cluster.ns)] = {"metadata": {}}
        admitted = {
            "status": {
                "ingress": [
                    {
                        "host": cluster.config["gateway"]["host"],
                        "conditions": [{"type": "Admitted", "status": "True"}],
                    }
                ]
            }
        }
        with (
            patch.object(cluster, "get", side_effect=[{}, admitted]),
            patch.object(installer.time, "sleep") as sleep,
        ):
            cluster.wait_for_route("openshell")
            sleep.assert_called_once()
        with (
            patch.object(installer.time, "monotonic", side_effect=[0, 601]),
            self.assertRaisesRegex(ValueError, "not admitted"),
        ):
            cluster.wait_for_route("openshell")

    def test_dashboard_route_waits_for_configured_host_admission(self):
        cluster = self.cluster()
        old = {
            "host": "old-dashboard.example.com",
            "conditions": [{"type": "Admitted", "status": "True"}],
        }
        current = {
            "host": cluster.config["dashboard"]["host"],
            "conditions": [{"type": "Admitted", "status": "False"}],
        }
        stale = {"status": {"ingress": [old, current]}}
        admitted = copy.deepcopy(stale)
        admitted["status"]["ingress"][1]["conditions"][0]["status"] = "True"
        with (
            patch.object(cluster, "get", side_effect=[stale, admitted]),
            patch.object(installer.time, "sleep") as sleep,
        ):
            cluster.wait_for_route("openshell-dashboard")
            sleep.assert_called_once()
        with (
            patch.object(cluster, "get", return_value=stale),
            patch.object(installer.time, "monotonic", side_effect=[0, 601]),
            self.assertRaisesRegex(ValueError, "not admitted"),
        ):
            cluster.wait_for_route("openshell-dashboard")

    def test_render_failure_prevents_cluster_access(self):
        path = self.root / "config.json"
        path.write_text(json.dumps(self.config))
        commands = []

        def run(_instance, args, payload=None):
            self.assertIsNone(payload)
            if args[1:3] == ["config", "view"]:
                return json.dumps(
                    {
                        "current-context": "test-context",
                        "contexts": [
                            {"name": "test-context", "context": {"cluster": "cluster"}}
                        ],
                    }
                )
            commands.append(args)
            if args[:2] == ["helm", "pull"]:
                destination = Path(args[args.index("--destination") + 1])
                (destination / "chart.tgz").write_bytes(b"fake")
                return ""
            raise RuntimeError("invalid chart values")

        with (
            patch("sys.argv", ["install.py", "install", "--config", str(path)]),
            patch.object(installer.shutil, "which", return_value="tool"),
            patch.object(installer.Installer, "run", run),
            self.assertRaisesRegex(RuntimeError, "invalid chart values"),
        ):
            installer.main()
        self.assertTrue(all(args[0] == "helm" for args in commands))

    def test_validate_never_calls_kubectl(self):
        path = self.root / "config.json"
        path.write_text(json.dumps(self.config))
        for chart in ("gateway", "dashboard"):
            (self.root / chart).mkdir()
            (self.root / chart / "Chart.yaml").write_text("version: 0.0.0\n")
        argv = [
            "install.py",
            "validate",
            "--config",
            str(path),
            "--namespace",
            "render-project",
            "--gateway-chart-directory",
            str(self.root / "gateway"),
            "--dashboard-chart-directory",
            str(self.root / "dashboard"),
        ]
        with (
            patch("sys.argv", argv),
            patch.object(installer.shutil, "which", return_value="helm"),
            patch.object(installer.Installer, "run", return_value="") as run,
        ):
            installer.main()
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][:2], ["helm", "template"])
            args = call.args[0]
            self.assertEqual(args[args.index("--namespace") + 1], "render-project")

    # --- lifecycle CLI ---------------------------------------------------

    @contextmanager
    def working_directory(self, directory=None):
        original = Path.cwd()
        os.chdir(directory or self.root)
        try:
            yield Path.cwd()
        finally:
            os.chdir(original)

    def run_main(self, argv):
        output = io.StringIO()
        with patch("sys.argv", ["install.py", *argv]), redirect_stdout(output):
            installer.main()
        return output.getvalue()

    def init(self, *args):
        return self.run_main(["init", *args])

    def installed_cluster(self):
        cluster = self.cluster()
        cluster.objects[("namespace", cluster.ns)] = {
            "metadata": {"labels": {installer.OWNER: installer.OWNER_VALUE}}
        }
        cluster.install(["g.tgz", "d.tgz"], ["g.json", "d.json"])
        cluster.objects[("secret", "openshell-server-tls")] = {"data": {}}
        cluster.objects[("secret", "openshell-jwt-keys")] = {"data": {}}
        cluster.calls.clear()
        return cluster

    @staticmethod
    def uninstall(cluster):
        output = io.StringIO()
        with redirect_stdout(output):
            try:
                cluster.uninstall(assume_yes=True)
            except ValueError as error:
                return output.getvalue(), error
        return output.getvalue(), None

    def deletion_sequence(self, cluster):
        sequence = []
        for args, _ in cluster.calls:
            if args[:2] == ["helm", "uninstall"]:
                sequence.append(args[2])
            if args[0] == "kubectl" and "delete" in args:
                sequence.append(args[args.index("delete") + 2])
        return sequence

    def test_bare_invocation_and_lone_config_flag_are_usage_errors(self):
        for argv in (["install.py"], ["install.py", "--config", "config.json"]):
            with (
                self.subTest(argv=argv),
                patch("sys.argv", argv),
                self.assertRaises(SystemExit) as caught,
            ):
                installer.main()
            self.assertEqual(caught.exception.code, 2)

    def test_stale_config_fields_get_migration_messages(self):
        self.config["context"] = "old-context"
        with self.assertRaisesRegex(ValueError, "no longer a configuration field"):
            self.load()
        self.config = configuration()
        self.config["gateway"]["values_files"] = []
        with self.assertRaisesRegex(ValueError, "renamed to helm_overrides"):
            self.load()

    def test_missing_default_config_names_init(self):
        with (
            self.working_directory(self.root),
            patch("sys.argv", ["install.py", "preflight"]),
            patch.object(installer.shutil, "which", return_value="tool"),
            self.assertRaisesRegex(ValueError, "No config.json in this directory"),
        ):
            installer.main()

    def test_config_defaults_to_config_json_and_prints_resolved_path(self):
        path = self.root / "config.json"
        path.write_text(json.dumps(self.config))
        commands = []

        def run(_instance, args, payload=None):
            self.assertIsNone(payload)
            commands.append(args)
            if args[:2] == ["helm", "pull"]:
                (Path(args[args.index("--destination") + 1]) / "chart.tgz").write_bytes(
                    b"fake"
                )
            return ""

        with (
            self.working_directory(self.root),
            patch("sys.argv", ["install.py", "validate"]),
            patch.object(installer.shutil, "which", return_value="tool"),
            patch.object(installer.Installer, "run", run),
            redirect_stdout(io.StringIO()) as output,
        ):
            installer.main()
        self.assertIn(f"Config file: {path.resolve()}", output.getvalue())
        self.assertEqual(len(commands), 4)

    def test_config_reading_cluster_commands_print_the_resolved_path(self):
        path = self.root / "config.json"
        path.write_text(json.dumps(self.config))

        def run(_instance, args, _payload=None):
            if args[1:3] == ["config", "view"]:
                return json.dumps(
                    {
                        "current-context": "ctx",
                        "contexts": [{"name": "ctx", "context": {"cluster": "c"}}],
                    }
                )
            if args[:2] == ["helm", "pull"]:
                (Path(args[args.index("--destination") + 1]) / "chart.tgz").write_bytes(
                    b"fake"
                )
            return ""

        for command in ("preflight", "install"):
            with (
                self.subTest(command=command),
                patch("sys.argv", ["install.py", command, "--config", str(path)]),
                patch.object(installer.shutil, "which", return_value="tool"),
                patch.object(installer.Installer, "run", run),
                patch.object(installer.Installer, "preflight", return_value=None),
                patch.object(installer.Installer, "install", return_value=None),
                redirect_stdout(io.StringIO()) as output,
            ):
                installer.main()
            text = output.getvalue()
            self.assertIn(f"Config file: {path.resolve()}", text)
            self.assertIn(self.config["oidc"]["issuer"], text)
            self.assertIn(self.config["oidc"]["audience"], text)
            self.assertIn(self.config["oidc"]["client_id"], text)
            self.assertIn(
                f"https://{self.config['dashboard']['host']}/oauth2/callback",
                text,
            )

    def test_context_flag_reaches_cluster_calls_and_not_local_helm(self):
        secret = self.root / "client.txt"
        secret.write_text("secret\n")
        self.config["oidc"]["client_secret_file"] = str(secret)
        path = self.root / "config.json"
        path.write_text(json.dumps(self.config))
        created = []

        class Recording(FakeCluster):
            def __init__(self, config, timeout="10m", context=None):
                super().__init__(config, timeout, context)
                self.objects[("namespace", self.ns)] = {"metadata": {}}
                created.append(self)

        with (
            patch(
                "sys.argv",
                ["install.py", "preflight", "--config", str(path), "--context", "ctx"],
            ),
            patch.object(installer.shutil, "which", return_value="tool"),
            patch.object(installer, "Installer", Recording),
        ):
            installer.main()
        for args, _ in created[0].calls:
            if args[0] == "kubectl" and args[1:3] != ["config", "view"]:
                self.assertIn("--context", args)
                self.assertEqual(args[args.index("--context") + 1], "ctx")
            if args[:2] in (["helm", "pull"], ["helm", "template"]):
                self.assertNotIn("--kube-context", args)
            elif args[0] == "helm":
                self.assertIn("--kube-context", args)

    def test_cluster_commands_refuse_empty_current_context(self):
        cluster = self.cluster()
        cluster.current_context = ""
        with self.assertRaisesRegex(ValueError, "No kubectl context is set"):
            cluster.audit_context()

    def test_audit_identity_shows_resolved_values_without_rewriting(self):
        cluster = self.cluster()
        cluster.ns = "override-team"
        with redirect_stdout(io.StringIO()) as output:
            cluster.audit_identity()
        text = output.getvalue()
        self.assertIn(cluster.config["oidc"]["issuer"], text)
        self.assertIn(cluster.config["oidc"]["audience"], text)
        self.assertIn(cluster.config["oidc"]["client_id"], text)
        self.assertIn(
            f"https://{cluster.config['dashboard']['host']}/oauth2/callback", text
        )
        self.assertIn("override-team", text)
        self.assertEqual(
            cluster.config["oidc"]["client_id"], "openshell-dashboard-openshell-team"
        )

    # --- uninstall -------------------------------------------------------

    def test_uninstall_refuses_unowned_namespace_before_any_deletion(self):
        cluster = self.cluster()
        cluster.objects[("namespace", cluster.ns)] = {
            "metadata": {"labels": {"team": "research"}}
        }
        before = copy.deepcopy(cluster.objects)
        _, error = self.uninstall(cluster)
        self.assertIsNotNone(error)
        self.assertIn("not installed by this installer", str(error))
        self.assertEqual(cluster.objects, before)
        self.assertFalse(any(args[0] == "helm" for args, _ in cluster.calls))
        self.assertFalse(any("delete" in args for args, _ in cluster.calls))

    def test_uninstall_removes_releases_then_secrets_with_state_last(self):
        cluster = self.installed_cluster()
        _, error = self.uninstall(cluster)
        self.assertIsNone(error)
        self.assertEqual(
            self.deletion_sequence(cluster),
            [
                "openshell-dashboard",
                "openshell",
                *installer.TLS_SECRETS,
                installer.OIDC_SECRET,
                installer.INSTALL_STATE,
            ],
        )
        self.assertIn(("namespace", cluster.ns), cluster.objects)
        self.assertNotIn(("configmap", installer.INSTALL_STATE), cluster.objects)
        self.assertNotIn(("secret", installer.OIDC_SECRET), cluster.objects)

    def test_uninstall_with_absent_namespace_is_a_no_op(self):
        cluster = self.cluster()
        output, error = self.uninstall(cluster)
        self.assertIsNone(error)
        self.assertIn("nothing to uninstall", output)
        self.assertFalse(any(args[0] == "helm" for args, _ in cluster.calls))

    def test_uninstall_requires_yes_without_a_terminal(self):
        cluster = self.installed_cluster()
        before = copy.deepcopy(cluster.objects)
        with (
            patch("sys.stdin.isatty", return_value=False),
            redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(ValueError, "--yes"),
        ):
            cluster.uninstall()
        self.assertEqual(cluster.objects, before)
        self.assertFalse(
            any(args[:2] == ["helm", "uninstall"] for args, _ in cluster.calls)
        )

    def test_uninstall_prompt_requires_typing_the_namespace(self):
        cluster = self.installed_cluster()
        before = copy.deepcopy(cluster.objects)
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="wrong"),
            redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(ValueError, "did not match"),
        ):
            cluster.uninstall()
        self.assertEqual(cluster.objects, before)

    def test_dashboard_removal_failure_skips_all_dependent_cleanup(self):
        cluster = self.installed_cluster()
        cluster.fail_dashboard = True
        output, error = self.uninstall(cluster)
        self.assertIn("uninstall incomplete", str(error))
        self.assertIn("Retained resources", output)
        self.assertEqual(
            {release["name"] for release in cluster.releases},
            {"openshell", "openshell-dashboard"},
        )
        for key in (
            ("configmap", installer.INSTALL_STATE),
            ("secret", installer.OIDC_SECRET),
            ("secret", "openshell-server-tls"),
        ):
            self.assertIn(key, cluster.objects)
        # Only the attempted (failed) dashboard removal is recorded; nothing
        # else was touched.
        self.assertEqual(self.deletion_sequence(cluster), ["openshell-dashboard"])

    def test_gateway_removal_failure_skips_tls_and_credentials(self):
        cluster = self.installed_cluster()
        cluster.fail_gateway = True
        _, error = self.uninstall(cluster)
        self.assertIn("uninstall incomplete", str(error))
        self.assertEqual(
            [release["name"] for release in cluster.releases], ["openshell"]
        )
        for key in (
            ("configmap", installer.INSTALL_STATE),
            ("secret", installer.OIDC_SECRET),
            ("secret", "openshell-server-tls"),
        ):
            self.assertIn(key, cluster.objects)
        self.assertEqual(
            self.deletion_sequence(cluster),
            ["openshell-dashboard", "openshell"],
        )

    def test_secret_deletion_failure_preserves_state_and_rerun_completes(self):
        cluster = self.installed_cluster()
        cluster.fail_delete = {"openshell-server-tls"}
        _, error = self.uninstall(cluster)
        self.assertIn("uninstall incomplete", str(error))
        self.assertIn(("configmap", installer.INSTALL_STATE), cluster.objects)
        self.assertIn(("secret", "openshell-server-tls"), cluster.objects)
        # Independent cleanup still ran.
        self.assertNotIn(("secret", installer.OIDC_SECRET), cluster.objects)
        cluster.fail_delete = set()
        cluster.calls.clear()
        _, error = self.uninstall(cluster)
        self.assertIsNone(error)
        self.assertNotIn(("configmap", installer.INSTALL_STATE), cluster.objects)
        self.assertNotIn(("secret", "openshell-server-tls"), cluster.objects)

    def test_credential_deletion_failure_preserves_state_and_rerun_completes(self):
        cluster = self.installed_cluster()
        cluster.fail_delete = {installer.OIDC_SECRET}
        _, error = self.uninstall(cluster)
        self.assertIn("openshell-dashboard-oidc", str(error))
        self.assertIn(("configmap", installer.INSTALL_STATE), cluster.objects)
        cluster.fail_delete = set()
        _, error = self.uninstall(cluster)
        self.assertIsNone(error)
        self.assertNotIn(("configmap", installer.INSTALL_STATE), cluster.objects)
        self.assertNotIn(("secret", installer.OIDC_SECRET), cluster.objects)

    def test_uninstall_works_from_namespace_alone_without_a_config_line(self):
        def run(_instance, args, _payload=None):
            if args[1:3] == ["config", "view"]:
                return json.dumps(
                    {
                        "current-context": "ctx",
                        "contexts": [{"name": "ctx", "context": {"cluster": "c"}}],
                    }
                )
            return ""

        with (
            self.working_directory(self.root),
            patch("sys.argv", ["install.py", "uninstall", "--namespace", "team-a"]),
            patch.object(installer.shutil, "which", return_value="tool"),
            patch.object(installer.Installer, "run", run),
            redirect_stdout(io.StringIO()) as output,
        ):
            installer.main()
        self.assertIn("nothing to uninstall", output.getvalue())
        self.assertNotIn("Config file:", output.getvalue())

    def test_uninstall_with_default_config_prints_the_config_line(self):
        path = self.root / "config.json"
        path.write_text(json.dumps(self.config))

        def run(_instance, args, _payload=None):
            if args[1:3] == ["config", "view"]:
                return json.dumps(
                    {
                        "current-context": "ctx",
                        "contexts": [{"name": "ctx", "context": {"cluster": "c"}}],
                    }
                )
            return ""

        with (
            self.working_directory(self.root),
            patch("sys.argv", ["install.py", "uninstall"]),
            patch.object(installer.shutil, "which", return_value="tool"),
            patch.object(installer.Installer, "run", run),
            redirect_stdout(io.StringIO()) as output,
        ):
            installer.main()
        self.assertIn(f"Config file: {path.resolve()}", output.getvalue())

    def test_uninstall_without_config_or_namespace_errors_naming_both(self):
        empty = self.root / "empty"
        empty.mkdir()
        with (
            self.working_directory(empty),
            patch("sys.argv", ["install.py", "uninstall"]),
            patch.object(installer.shutil, "which", return_value="tool"),
            self.assertRaisesRegex(ValueError, "--namespace or --config"),
        ):
            installer.main()

    def test_uninstall_reports_cluster_scoped_rbac_survivors(self):
        cluster = self.installed_cluster()
        cluster.cluster_rbac = [
            {"metadata": {"name": "node-reader"}},
            {"metadata": {"name": "node-reader"}},
            {"metadata": {"name": "dashboard-reader"}},
        ]
        output, error = self.uninstall(cluster)
        self.assertIsNone(error)
        self.assertIn(
            "cluster-scoped RBAC left in place: dashboard-reader, node-reader", output
        )
        self.assertNotIn("could not check", output)

    def test_external_credentials_are_never_created_or_deleted(self):
        self.config["oidc"]["client_secret_secret"] = "admin-oidc"
        self.config["oidc"]["ca_configmap"] = "admin-ca"
        cluster = FakeCluster(self.load())
        cluster.objects[("namespace", cluster.ns)] = {
            "metadata": {"labels": {installer.OWNER: installer.OWNER_VALUE}}
        }
        cluster.objects[("secret", "admin-oidc")] = {
            "data": {
                "client-secret": base64.b64encode(b"s").decode(),
                "cookie-secret": base64.b64encode(b"c").decode(),
            }
        }
        cluster.objects[("configmap", "admin-ca")] = {"data": {"ca.crt": "ca"}}
        cluster.install(["g.tgz", "d.tgz"], ["g.json", "d.json"])
        self.assertNotIn(("secret", installer.OIDC_SECRET), cluster.objects)
        self.assertNotIn(("configmap", installer.OIDC_CA), cluster.objects)
        gateway, dashboard = installer.chart_values(cluster.config)
        self.assertEqual(dashboard["oidc"]["existingSecret"], "admin-oidc")
        self.assertEqual(gateway["server"]["oidc"]["caConfigMapName"], "admin-ca")
        cluster.calls.clear()
        _, error = self.uninstall(cluster)
        self.assertIsNone(error)
        self.assertIn(("secret", "admin-oidc"), cluster.objects)
        self.assertIn(("configmap", "admin-ca"), cluster.objects)
        self.assertNotIn(("configmap", installer.INSTALL_STATE), cluster.objects)

    def test_external_secret_reusing_fixed_names_is_preserved(self):
        self.config["oidc"]["client_secret_secret"] = installer.OIDC_SECRET
        cluster = FakeCluster(self.load())
        cluster.objects[("namespace", cluster.ns)] = {
            "metadata": {"labels": {installer.OWNER: installer.OWNER_VALUE}}
        }
        secret = {
            "data": {
                "client-secret": base64.b64encode(b"s").decode(),
                "cookie-secret": base64.b64encode(b"c").decode(),
            }
        }
        cluster.objects[("secret", installer.OIDC_SECRET)] = copy.deepcopy(secret)
        cluster.install(["g.tgz", "d.tgz"], ["g.json", "d.json"])
        self.assertEqual(cluster.objects[("secret", installer.OIDC_SECRET)], secret)
        _, error = self.uninstall(cluster)
        self.assertIsNone(error)
        self.assertEqual(cluster.objects[("secret", installer.OIDC_SECRET)], secret)

    def test_preflight_rejects_external_secret_missing_keys(self):
        self.config["oidc"]["client_secret_secret"] = "admin-oidc"
        cluster = FakeCluster(self.load())
        cluster.objects[("namespace", cluster.ns)] = {"metadata": {}}
        cluster.objects[("secret", "admin-oidc")] = {
            "data": {"client-secret": base64.b64encode(b"s").decode()}
        }
        with self.assertRaisesRegex(ValueError, "cookie-secret"):
            cluster.preflight()

    def test_client_secret_file_and_secret_reference_are_mutually_exclusive(self):
        self.config["oidc"]["client_secret_secret"] = "admin-oidc"
        (self.root / "secret.txt").write_text("s")
        self.config["oidc"]["client_secret_file"] = "secret.txt"
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            self.load()

    # --- init ------------------------------------------------------------

    def test_init_writes_the_fixed_file_set(self):
        work = self.root / "work"
        work.mkdir()
        with self.working_directory(work):
            output = self.init()
        for name in (
            "config.json",
            "gateway-overrides.yaml",
            "dashboard-overrides.yaml",
        ):
            path = work / name
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIn("upstream chart images", output)
        config = json.loads((work / "config.json").read_text())
        self.assertNotIn("images", config["gateway"])
        self.assertEqual(
            config["gateway"]["version"], "REPLACE_WITH_EXACT_OPENSHELL_VERSION"
        )
        config["gateway"]["version"] = "0.1.3"
        config["dashboard"]["version"] = "0.1.0"
        (work / "openshell-dashboard-oidc-secret.txt").write_text("secret\n")
        path = work / "config.json"
        path.write_text(json.dumps(config))
        loaded = installer.load_config(path)
        self.assertEqual(
            loaded["gateway"]["helm_overrides"], ["gateway-overrides.yaml"]
        )

    def test_init_refuses_to_overwrite_and_writes_nothing(self):
        work = self.root / "work"
        work.mkdir()
        (work / "gateway-overrides.yaml").write_text("existing")
        with (
            self.working_directory(work),
            self.assertRaisesRegex(ValueError, "already exists"),
        ):
            self.init()
        self.assertEqual((work / "gateway-overrides.yaml").read_text(), "existing")
        self.assertFalse((work / "config.json").exists())
        self.assertFalse((work / "dashboard-overrides.yaml").exists())

    def test_embedded_templates_match_the_example_files(self):
        self.assertEqual(
            installer.CONFIG_TEMPLATE,
            Path(__file__).with_name("config.example.json").read_text(),
        )
        self.assertEqual(
            installer.GATEWAY_OVERRIDES_TEMPLATE,
            Path(__file__).with_name("gateway-overrides.example.yaml").read_text(),
        )
        self.assertEqual(
            installer.DASHBOARD_OVERRIDES_TEMPLATE,
            Path(__file__).with_name("dashboard-overrides.example.yaml").read_text(),
        )

    def test_init_flags_build_a_loadable_rhoai_config(self):
        work = self.root / "work"
        work.mkdir()
        with self.working_directory(work):
            self.init(
                "--namespace",
                "team-a",
                "--cluster-app-domain",
                "apps.example.com",
                "--rhoai-version",
                "3.6",
                "--oidc-issuer",
                "https://identity.example.com/realms/customer",
            )
        config = json.loads((work / "config.json").read_text())
        self.assertEqual(config["namespace"], "team-a")
        self.assertEqual(config["oidc"]["client_id"], "openshell-dashboard-team-a")
        self.assertEqual(config["oidc"]["audience"], "openshell-api-team-a")
        self.assertEqual(config["gateway"]["host"], "gateway-team-a.apps.example.com")
        self.assertEqual(
            config["dashboard"]["host"], "dashboard-team-a.apps.example.com"
        )
        self.assertEqual(config["gateway"]["version"], "0.1.3")
        self.assertEqual(config["dashboard"]["version"], "0.1.0")
        self.assertEqual(
            config["gateway"]["images"]["gateway"]["repository"],
            "rhoai/odh-openshell-gateway-rhel9",
        )
        self.assertEqual(
            config["gateway"]["images"]["sandbox"]["repository"],
            "registry.redhat.io/rhoai/odh-openshell-sandbox-rhel9",
        )
        self.assertEqual(
            config["dashboard"]["images"]["authProxy"],
            installer.AUTH_PROXY_IMAGE,
        )
        self.assertIn("rhoai-3.6", json.dumps(config["gateway"]["images"]))
        (work / "openshell-dashboard-oidc-secret.txt").write_text("secret\n")
        installer.load_config(work / "config.json")

    def test_init_cluster_app_domain_derives_hosts_and_keeps_placeholders(self):
        work = self.root / "work"
        work.mkdir()
        with self.working_directory(work):
            self.init(
                "--namespace", "team-a", "--cluster-app-domain", "apps.example.com"
            )
        config = json.loads((work / "config.json").read_text())
        self.assertEqual(config["gateway"]["host"], "gateway-team-a.apps.example.com")
        self.assertEqual(
            config["dashboard"]["host"], "dashboard-team-a.apps.example.com"
        )
        self.assertTrue(config["gateway"]["version"].startswith("REPLACE_WITH"))

    def test_init_explicit_hosts_override_derivation(self):
        work = self.root / "work"
        work.mkdir()
        with self.working_directory(work):
            self.init(
                "--namespace",
                "team-a",
                "--cluster-app-domain",
                "apps.example.com",
                "--gateway-host",
                "gw.example.com",
                "--dashboard-host",
                "dash.example.com",
            )
        config = json.loads((work / "config.json").read_text())
        self.assertEqual(config["gateway"]["host"], "gw.example.com")
        self.assertEqual(config["dashboard"]["host"], "dash.example.com")

    def test_init_rejects_invalid_domains_and_versions_before_writing(self):
        for flag, value in (
            ("--cluster-app-domain", "https://apps.example.com"),
            ("--cluster-app-domain", "apps.example.com:443"),
            ("--cluster-app-domain", "apps.example.com/path"),
            ("--cluster-app-domain", "*.apps.example.com"),
            ("--cluster-app-domain", "apps..example.com"),
            ("--cluster-app-domain", "apps.example.com."),
            ("--openshell-version", "0.0.0"),
            ("--openshell-version", ">=0.1.0"),
            ("--openshell-version", "latest"),
        ):
            with self.subTest(flag=flag, value=value):
                work = self.root / f"invalid-{abs(hash((flag, value)))}"
                work.mkdir()
                with (
                    self.working_directory(work),
                    self.assertRaises(ValueError),
                ):
                    self.init(flag, value)
                self.assertFalse((work / "config.json").exists())

    def test_init_long_namespace_requires_explicit_hosts(self):
        namespace = "a" * 60
        work = self.root / "long"
        work.mkdir()
        with (
            self.working_directory(work),
            self.assertRaisesRegex(ValueError, "exceeds 63"),
        ):
            self.init(
                "--namespace",
                namespace,
                "--cluster-app-domain",
                "apps.example.com",
            )
        self.assertFalse((work / "config.json").exists())
        with self.working_directory(work):
            self.init(
                "--namespace",
                namespace,
                "--gateway-host",
                "gw.example.com",
                "--dashboard-host",
                "dash.example.com",
            )
        self.assertTrue((work / "config.json").is_file())

    def test_two_namespaces_cannot_share_derived_hosts(self):
        hosts = []
        for namespace in ("team-a", "team-b"):
            work = self.root / namespace
            work.mkdir()
            with self.working_directory(work):
                self.init(
                    "--namespace",
                    namespace,
                    "--cluster-app-domain",
                    "apps.example.com",
                )
            config = json.loads((work / "config.json").read_text())
            hosts.append((config["gateway"]["host"], config["dashboard"]["host"]))
        self.assertNotEqual(hosts[0], hosts[1])

    def test_from_config_seeds_shared_identity_only(self):
        source = self.root / "source"
        source.mkdir()
        (source / "ca.pem").write_text("ca")
        oidc = {
            "provider": "keycloak-oidc",
            "issuer": "https://identity.example.com/realms/customer",
            "ca_file": "ca.pem",
            "client_id": "source-client",
            "audience": "source-audience",
            "roles_claim": "realm_access.roles",
            "admin_role": "openshell-admin",
            "user_role": "openshell-user",
            "allowed_groups": ["source-group"],
            "allowed_roles": ["openshell-admin", "openshell-user"],
            "scope": "openid email profile",
        }
        (source / "config.json").write_text(json.dumps({"oidc": oidc}))
        work = self.root / "team-b"
        work.mkdir()
        with self.working_directory(work):
            self.init(
                "--from-config",
                str(source / "config.json"),
                "--namespace",
                "team-b",
                "--cluster-app-domain",
                "apps.example.com",
            )
        text = (work / "config.json").read_text()
        config = json.loads(text)
        self.assertEqual(config["oidc"]["provider"], "keycloak-oidc")
        self.assertEqual(config["oidc"]["roles_claim"], "realm_access.roles")
        self.assertEqual(config["oidc"]["scope"], "openid email profile")
        self.assertEqual(config["oidc"]["ca_file"], str((source / "ca.pem").resolve()))
        self.assertEqual(config["oidc"]["client_id"], "openshell-dashboard-team-b")
        self.assertEqual(config["oidc"]["audience"], "openshell-api-team-b")
        # Deployment-specific access assignments are not copied; the template's
        # neutral empty lists remain.
        self.assertEqual(config["oidc"]["allowed_groups"], [])
        self.assertEqual(config["oidc"]["allowed_roles"], [])
        self.assertNotEqual(config["oidc"]["client_id"], oidc["client_id"])
        self.assertNotEqual(config["oidc"]["audience"], oidc["audience"])
        # Self-contained: no reference to the source config remains (the CA is a
        # copied absolute path to the administrator-managed file).
        self.assertNotIn(str(source / "config.json"), text)

    def test_from_config_rejects_a_source_missing_a_shared_key(self):
        source = self.root / "source.json"
        source.write_text(
            json.dumps(
                {"oidc": {"roles_claim": "roles", "admin_role": "a", "user_role": "u"}}
            )
        )
        work = self.root / "work"
        work.mkdir()
        with (
            self.working_directory(work),
            self.assertRaisesRegex(ValueError, "Missing oidc.issuer"),
        ):
            self.init("--from-config", str(source))
        self.assertFalse((work / "config.json").exists())

    def test_from_config_and_inline_shared_flags_are_mutually_exclusive(self):
        source = self.root / "source.json"
        source.write_text("{}")
        for flag in ("--oidc-issuer", "--oidc-ca-file", "--oidc-ca-configmap"):
            work = self.root / f"mutual-{flag.strip('-')}"
            work.mkdir()
            with (
                self.subTest(flag=flag),
                self.working_directory(work),
                self.assertRaisesRegex(ValueError, "combined"),
            ):
                self.init("--from-config", str(source), flag, "value")
            self.assertFalse((work / "config.json").exists())

    def test_inline_shared_flags_compose(self):
        work = self.root / "work"
        work.mkdir()
        (work / "ca.pem").write_text("ca")
        with self.working_directory(work):
            self.init(
                "--oidc-issuer",
                "https://identity.example.com/realms/customer",
                "--oidc-ca-file",
                "ca.pem",
            )
        config = json.loads((work / "config.json").read_text())
        self.assertEqual(
            config["oidc"]["issuer"], "https://identity.example.com/realms/customer"
        )
        self.assertEqual(config["oidc"]["ca_file"], "ca.pem")

    def test_explicit_chart_versions_compose(self):
        work = self.root / "work"
        work.mkdir()
        with self.working_directory(work):
            self.init(
                "--openshell-version", "0.1.3", "--openshell-dashboard-version", "0.1.0"
            )
        config = json.loads((work / "config.json").read_text())
        self.assertEqual(config["gateway"]["version"], "0.1.3")
        self.assertEqual(config["dashboard"]["version"], "0.1.0")

    def test_provider_neutrality(self):
        self.config["oidc"]["provider"] = "keycloak-oidc"
        self.config["oidc"]["allowed_groups"] = ["group"]
        self.config["oidc"]["allowed_roles"] = ["role"]
        loaded = self.load()
        self.assertEqual(loaded["oidc"]["allowed_groups"], ["group"])
        self.assertEqual(loaded["oidc"]["allowed_roles"], ["role"])
        self.assertEqual(loaded["oidc"]["roles_claim"], "roles")
        self.config["oidc"]["provider"] = "oidc"
        with self.assertRaisesRegex(ValueError, "allowed_groups"):
            self.load()

    # --- RHOAI profiles --------------------------------------------------

    @staticmethod
    def synthetic_profile(**overrides):
        profile = {
            "aliases": (),
            "gateway_chart": "oci://example.test/gateway",
            "gateway_version": "9.9.9",
            "dashboard_chart": "oci://example.test/dashboard",
            "dashboard_version": "8.8.8",
            "image_tag": "rhoai-ea",
            "prerelease": True,
            "images_qualified": False,
        }
        profile.update(overrides)
        return profile

    def test_36_alias_resolves_to_the_ga_profile(self):
        canonical, profile = installer.resolve_profile("3.6")
        self.assertEqual(canonical, "3.6.0")
        self.assertEqual(profile["gateway_version"], "0.1.3")
        self.assertEqual(profile["dashboard_version"], "0.1.0")
        self.assertEqual(profile["image_tag"], "rhoai-3.6")

    def test_synthetic_ea_profile_resolves_and_rejects_a_bare_line(self):
        with patch.dict(
            installer.RHOAI_PROFILES,
            {"3.7.0-ea1": self.synthetic_profile()},
            clear=False,
        ):
            canonical, profile = installer.resolve_profile("3.7.0-EA1")
            self.assertEqual(canonical, "3.7.0-ea1")
            self.assertTrue(profile["prerelease"])
            with self.assertRaisesRegex(ValueError, "supported"):
                installer.resolve_profile("3.7")
            with self.assertRaisesRegex(ValueError, "supported"):
                installer.resolve_profile("3.7.1")
            installer.RHOAI_PROFILES["3.7.0"] = self.synthetic_profile(
                aliases=("3.7",), prerelease=False
            )
            canonical, profile = installer.resolve_profile("3.7")
            self.assertEqual(canonical, "3.7.0")
            self.assertFalse(profile["prerelease"])

    def test_unknown_release_fails_before_writing(self):
        work = self.root / "work"
        work.mkdir()
        with (
            self.working_directory(work),
            self.assertRaisesRegex(ValueError, "supported"),
        ):
            self.init("--rhoai-version", "3.7")
        self.assertFalse((work / "config.json").exists())

    def test_profile_and_explicit_versions_are_mutually_exclusive(self):
        for flag in ("--openshell-version", "--openshell-dashboard-version"):
            work = self.root / f"exclusive-{flag.strip('-')}"
            work.mkdir()
            with (
                self.subTest(flag=flag),
                self.working_directory(work),
                self.assertRaisesRegex(ValueError, "combined"),
            ):
                self.init("--rhoai-version", "3.6", flag, "0.1.3")
            self.assertFalse((work / "config.json").exists())

    def test_unqualified_profile_warns_and_qualified_does_not(self):
        work = self.root / "work"
        work.mkdir()
        with self.working_directory(work):
            output = self.init("--rhoai-version", "3.6")
        self.assertIn("not been validated", output)
        qualified = self.root / "qualified"
        qualified.mkdir()
        with (
            patch.dict(
                installer.RHOAI_PROFILES,
                {
                    "3.6.0": dict(
                        installer.RHOAI_PROFILES["3.6.0"], images_qualified=True
                    )
                },
            ),
            self.working_directory(qualified),
        ):
            output = self.init("--rhoai-version", "3.6")
        self.assertNotIn("not been validated", output)

    def test_profiles_embed_no_image_digests(self):
        for key, profile in installer.RHOAI_PROFILES.items():
            with self.subTest(profile=key):
                self.assertNotIn("sha256:", json.dumps(profile))
                self.assertNotIn("digest", json.dumps(profile))

    def test_readme_compatibility_table_matches_the_profiles(self):
        readme = Path(__file__).with_name("README.md").read_text()
        for key, profile in installer.RHOAI_PROFILES.items():
            with self.subTest(profile=key):
                self.assertIn(f"`{key}`", readme)
                self.assertIn(profile["gateway_version"], readme)
                self.assertIn(profile["dashboard_version"], readme)
                self.assertIn(f"`{profile['image_tag']}`", readme)
                alias = profile["aliases"][0]
                self.assertIn(f"--rhoai-version {alias}", readme)
                self.assertIn(profile["gateway_chart"], readme)

    # --- RHOAI image verification ---------------------------------------

    def rhoai_cluster(self):
        self.config = configuration()
        installer.apply_profile(self.config, installer.RHOAI_PROFILES["3.6.0"])
        return self.cluster()

    @staticmethod
    def pin_digests(config):
        for name, component, _ in installer.RHOAI_IMAGE_COMPONENTS:
            image = config[name]["images"][component]
            image["digest"] = f"sha256:{name}{component}"[:71]

    @staticmethod
    def csv_for(config):
        related = []
        for name, component, mapped in installer.RHOAI_IMAGE_COMPONENTS:
            image = config[name]["images"][component]
            repository = installer.image_repository(image)
            related.append(
                {
                    "name": mapped,
                    "image": f"{repository}@{image['digest']}",
                }
            )
        return [
            {
                "metadata": {"name": "rhods-operator.v3.6.0"},
                "spec": {"relatedImages": related},
            }
        ]

    def test_preflight_rejects_csv_digest_and_repository_mismatches(self):
        for field, value in (
            ("digest", "sha256:mismatch"),
            ("registry", "quay.io"),
        ):
            with self.subTest(field=field):
                cluster = self.rhoai_cluster()
                self.pin_digests(cluster.config)
                cluster.csvs = self.csv_for(cluster.config)
                cluster.config["gateway"]["images"]["gateway"][field] = value
                cluster.calls.clear()
                with self.assertRaises(ValueError):
                    cluster.preflight()
                self.assertFalse(
                    any(args[:2] == ["helm", "upgrade"] for args, _ in cluster.calls)
                )

    def test_preflight_warns_when_skopeo_is_unavailable(self):
        cluster = self.rhoai_cluster()
        self.pin_digests(cluster.config)
        cluster.csvs = self.csv_for(cluster.config)
        for name, component, _ in installer.RHOAI_IMAGE_COMPONENTS:
            cluster.config[name]["images"][component].pop("digest")
        with (
            patch.object(
                installer.shutil,
                "which",
                side_effect=lambda tool: None if tool == "skopeo" else tool,
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            cluster.preflight()
        self.assertIn("skopeo is not installed", output.getvalue())

    def test_preflight_warns_and_continues_without_an_rhoai_csv(self):
        cluster = self.rhoai_cluster()
        self.pin_digests(cluster.config)
        cluster.csvs = []
        with redirect_stdout(io.StringIO()) as output:
            cluster.preflight()
        self.assertIn("product images were not verified", output.getvalue())

    def test_validate_performs_no_csv_lookup(self):
        path = self.root / "config.json"
        self.config = configuration()
        installer.apply_profile(self.config, installer.RHOAI_PROFILES["3.6.0"])
        path.write_text(json.dumps(self.config))
        commands = []

        def run(_instance, args, _payload=None):
            commands.append(args)
            if args[:2] == ["helm", "pull"]:
                (Path(args[args.index("--destination") + 1]) / "chart.tgz").write_bytes(
                    b"fake"
                )
            return ""

        with (
            patch("sys.argv", ["install.py", "validate", "--config", str(path)]),
            patch.object(installer.shutil, "which", return_value="tool"),
            patch.object(installer.Installer, "run", run),
        ):
            installer.main()
        self.assertFalse(any("csv" in args for args in commands))

    # --- preflight host isolation ---------------------------------------

    def test_preflight_rejects_a_host_claimed_in_another_namespace(self):
        cluster = self.cluster()
        host = cluster.config["gateway"]["host"]
        cluster.routes = [
            {
                "metadata": {"name": "foreign", "namespace": "other-team"},
                "spec": {"host": host},
            }
        ]
        with self.assertRaisesRegex(ValueError, "other-team") as caught:
            cluster.preflight()
        self.assertIn(host, str(caught.exception))
        self.assertFalse(
            any(args[:2] == ["helm", "upgrade"] for args, _ in cluster.calls)
        )

    def test_failed_dashboard_upgrade_preserves_state(self):
        cluster = self.cluster()
        cluster.fail_dashboard = True
        with self.assertRaisesRegex(RuntimeError, "dashboard failed"):
            cluster.install(["g.tgz", "d.tgz"], ["g.json", "d.json"])
        self.assertIn(("configmap", installer.INSTALL_STATE), cluster.objects)
        self.assertEqual(
            {release["name"] for release in cluster.releases}, {"openshell"}
        )


if __name__ == "__main__":
    unittest.main()
