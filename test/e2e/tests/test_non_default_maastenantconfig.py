"""MaasTenantConfig settings on a non-default tenant (RHOAIENG-100759).

Existing coverage patches only the default tenant's MaasTenantConfig and checks the
default workloads. These tests patch a *non-default* tenant's config and assert the
override lands on that tenant's own resources without disturbing any sibling tenant.

Scope, one class per spec field group:

- ``spec.maasApi``             -> Deployment/maas-api-{tenant} in the infra namespace
- ``spec.payloadProcessing``   -> Deployment/payload-processing-{tenant} in the gateway namespace
- ``spec.apiKeys``             -> key-mint policy on that tenant's own maas-api
- ``spec.telemetry``           -> usage-logs EnvoyFilter wiring for that tenant

Payload processing is asserted against whichever dataplane the run is configured for
(``E2E_PAYLOAD_PROCESSING_TYPE``); both Praxis and legacy IPP render the same
``payload-processing-{tenant}`` Deployments and differ only in plugin ConfigMap keys
and container args.

Fields that a cluster's installed CRD does not declare are skipped with a linked
follow-up rather than failed: the API server prunes unknown fields, so the patch
appears to apply and the resource simply never converges.
"""

from __future__ import annotations

import contextlib
import json
import re
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from multitenancy_helpers import (
    GATEWAY_NAMESPACE,
    INFRA_NAMESPACE,
    MODEL_BACKEND_READY_TIMEOUT,
    TENANT_CONFIG_KIND,
    TENANT_CR_NAME,
    _oc_run,
    create_api_key_at,
    make_tenant_model_accessible,
    per_tenant_ipp_names,
    per_tenant_maas_api_names,
    provision_tenant_model,
    wait_for_json,
)
from test_helper import (
    _delete_cr,
    _delete_governance_and_wait,
    _get_cluster_token,
    _get_cr,
)

pytestmark = [pytest.mark.tenant_isolation, pytest.mark.xdist_group("tenant_isolation")]

MAAS_TENANT_CONFIG_CRD = "maastenantconfigs.maas.opendatahub.io"
USAGE_LOGS_ENVOY_FILTER_BASE = "maas-model-access-logs"

CONVERGENCE_TIMEOUT = 300

# Deliberately unusual values: a converged resource cannot be confused with a default.
MAAS_API_RESOURCE_REQUESTS: dict[str, str] = {"cpu": "123m", "memory": "234Mi"}
IPP_RESOURCE_REQUESTS: dict[str, str] = {"cpu": "137m", "memory": "251Mi"}

# Far below the platform default so a key the tenant must reject stays acceptable
# elsewhere, which is what makes the namespace-scoping check meaningful.
TENANT_MAX_EXPIRATION_DAYS = 7
EXCEEDS_TENANT_WITHIN_DEFAULT_DAYS = 30
HOURS_PER_DAY = 24

# Follow-ups for cases skipped because the installed CRD predates the field.
# Replace with dedicated issues if these outlive RHOAIENG-100759.
CRD_FIELD_FOLLOW_UP = "RHOAIENG-100759"

_FOLLOW_UP_RE = re.compile(r"(^[A-Z][A-Z0-9]+-\d+$)|(^https?://\S+$)")


def _skip_with_followup(reason: str, follow_up: str) -> None:
    """Skip, recording the issue that tracks un-skipping.

    Cases here run in the default E2E suite, where a skip does not fail CI and can
    sit unnoticed. Requiring a tracker reference keeps them from becoming permanent.
    """
    if not _FOLLOW_UP_RE.match(follow_up.strip()):
        raise ValueError(f"follow_up must be a Jira key or tracker URL, got {follow_up!r}")
    pytest.skip(f"{reason} [follow-up: {follow_up}]")


def _tenant_config_supports(*field_path: str) -> bool:
    """True when the installed MaasTenantConfig CRD declares spec.<field_path>.

    Walks the schema as plain dicts; a CRD can ship spec.telemetry while omitting
    spec.telemetry.logs, so nested paths must be checked, not just top-level names.
    """
    result = _oc_run(["get", "crd", MAAS_TENANT_CONFIG_CRD, "-o", "json"])
    if result.returncode != 0:
        return False
    crd = json.loads(result.stdout)
    for version in crd.get("spec", {}).get("versions", []):
        schema = version.get("schema", {}).get("openAPIV3Schema", {}).get("properties", {}).get("spec")
        for field_name in field_path:
            if not isinstance(schema, dict):
                schema = None
                break
            schema = (schema.get("properties") or {}).get(field_name)
        if schema is not None:
            return True
    return False


@contextlib.contextmanager
def _tenant_config_patched(tenant_namespace: str, spec_patch: dict[str, Any], *remove_paths: str) -> Iterator[None]:
    """Merge-patch a tenant's MaasTenantConfig and remove the added paths on exit."""
    body = json.dumps({"spec": spec_patch})
    result = _oc_run(
        ["patch", TENANT_CONFIG_KIND, TENANT_CR_NAME, "-n", tenant_namespace, "--type=merge", "-p", body]
    )
    assert result.returncode == 0, (
        f"Failed to patch {TENANT_CONFIG_KIND}/{TENANT_CR_NAME} in '{tenant_namespace}': "
        f"{(result.stderr or result.stdout or '').strip()}"
    )
    try:
        yield
    finally:
        removals = json.dumps([{"op": "remove", "path": path} for path in remove_paths])
        _oc_run(
            ["patch", TENANT_CONFIG_KIND, TENANT_CR_NAME, "-n", tenant_namespace, "--type=json", "-p", removals]
        )


def _container_resources(deployment: dict[str, Any]) -> dict[str, Any]:
    containers = (((deployment.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
    assert containers, f"Deployment/{(deployment.get('metadata') or {}).get('name')} has no containers"
    return containers[0].get("resources") or {}


def _snapshot_deployments(namespace: str, name_prefix: str) -> dict[str, dict[str, Any]]:
    """Return {name: {replicas, resources}} for Deployments matching a prefix."""
    result = _oc_run(["get", "deployment", "-n", namespace, "-o", "json"])
    assert result.returncode == 0, f"Failed to list deployments in '{namespace}': {result.stderr}"
    snapshot: dict[str, dict[str, Any]] = {}
    for item in json.loads(result.stdout).get("items", []):
        name = (item.get("metadata") or {}).get("name", "")
        if name.startswith(name_prefix):
            snapshot[name] = {
                "replicas": (item.get("spec") or {}).get("replicas"),
                "resources": _container_resources(item),
            }
    return snapshot


def _assert_only_changed(namespace: str, name_prefix: str, changed: str, before: dict[str, dict[str, Any]]) -> None:
    """Assert every prefix-matching Deployment except one still matches the snapshot.

    Stronger than checking a single sibling: it also catches an override that leaks
    onto another tenant's workload in the same namespace.
    """
    after = _snapshot_deployments(namespace, name_prefix)
    for name, previous in before.items():
        if name == changed:
            continue
        current = after.get(name)
        assert current is not None, f"Deployment/{name} in '{namespace}' disappeared during the patch"
        assert current == previous, (
            f"Deployment/{name} in '{namespace}' changed from {previous} to {current}; "
            f"only {changed} should have been affected"
        )


def _wait_for_replicas(name: str, namespace: str, expected: int) -> None:
    wait_for_json(
        "deployment",
        name,
        namespace,
        predicate=lambda obj: (obj.get("spec") or {}).get("replicas") == expected,
        timeout=CONVERGENCE_TIMEOUT,
    )


def _wait_for_resources(name: str, namespace: str, expected: dict[str, Any]) -> None:
    """Wait until the first container's resources contain the expected scopes.

    Only the provided scopes are compared, so setting requests alone does not have
    to restate whatever limits the renderer already applied.
    """

    def _matches(obj: dict[str, Any]) -> bool:
        observed = _container_resources(obj)
        return all(observed.get(scope) == values for scope, values in expected.items())

    wait_for_json("deployment", name, namespace, predicate=_matches, timeout=CONVERGENCE_TIMEOUT)


def _non_default_tenant(shared_test_tenants) -> dict[str, str]:
    """Return the tenant to patch (tenant B), leaving tenant A as the untouched sibling."""
    return shared_test_tenants[1]


class TestNonDefaultTenantMaasAPI:
    """spec.maasApi overrides apply to the tenant's own maas-api Deployment."""

    def test_replicas_override_applies_to_tenant_deployment_only(self, shared_test_tenants):
        """Given a non-default tenant's MaasTenantConfig,
        when spec.maasApi.replicas is patched,
        then maas-api-{tenant} converges and sibling maas-api Deployments do not change.
        """
        tenant = _non_default_tenant(shared_test_tenants)
        deployment_name = per_tenant_maas_api_names(tenant["name"])["deployment"]
        before = _snapshot_deployments(INFRA_NAMESPACE, "maas-api")
        assert deployment_name in before, f"Deployment/{deployment_name} not found; found {sorted(before)}"

        # Derive the target so the assertion stays meaningful on a cluster whose
        # rendered default already equals whatever literal we might have picked.
        target = (before[deployment_name]["replicas"] or 1) + 1

        with _tenant_config_patched(tenant["namespace"], {"maasApi": {"replicas": target}}, "/spec/maasApi"):
            _wait_for_replicas(deployment_name, INFRA_NAMESPACE, target)
            _assert_only_changed(INFRA_NAMESPACE, "maas-api", deployment_name, before)

    def test_resources_override_applies_to_tenant_deployment_only(self, shared_test_tenants):
        """Given a non-default tenant's MaasTenantConfig,
        when spec.maasApi.resources requests are patched,
        then maas-api-{tenant} converges and sibling maas-api Deployments do not change.
        """
        tenant = _non_default_tenant(shared_test_tenants)
        deployment_name = per_tenant_maas_api_names(tenant["name"])["deployment"]
        before = _snapshot_deployments(INFRA_NAMESPACE, "maas-api")
        assert deployment_name in before, f"Deployment/{deployment_name} not found; found {sorted(before)}"

        requests_patch = {"requests": MAAS_API_RESOURCE_REQUESTS}
        with _tenant_config_patched(
            tenant["namespace"], {"maasApi": {"resources": requests_patch}}, "/spec/maasApi"
        ):
            _wait_for_resources(deployment_name, INFRA_NAMESPACE, requests_patch)
            _assert_only_changed(INFRA_NAMESPACE, "maas-api", deployment_name, before)


class TestNonDefaultTenantPayloadProcessing:
    """spec.payloadProcessing overrides apply to the tenant's own IPP Deployments.

    Asserted against whichever dataplane the run configures; Praxis and legacy IPP
    render the same payload-processing-{tenant} Deployments.
    """

    def test_resources_override_applies_to_tenant_deployment_only(self, shared_test_tenants):
        """Given a non-default tenant's MaasTenantConfig,
        when spec.payloadProcessing.resources is patched,
        then payload-processing-{tenant} converges and sibling IPP Deployments do not change.
        """
        tenant = _non_default_tenant(shared_test_tenants)
        deployment_name = per_tenant_ipp_names(tenant["name"])["processing_deployment"]
        before = _snapshot_deployments(GATEWAY_NAMESPACE, "payload-processing")
        if deployment_name not in before:
            _skip_with_followup(
                reason=(
                    f"Deployment/{deployment_name} not present in '{GATEWAY_NAMESPACE}'; this tenant has no "
                    f"payload-processing workload for spec.payloadProcessing to act on. Found {sorted(before)}."
                ),
                follow_up=CRD_FIELD_FOLLOW_UP,
            )

        requests_patch = {"requests": IPP_RESOURCE_REQUESTS}
        with _tenant_config_patched(
            tenant["namespace"], {"payloadProcessing": {"resources": requests_patch}}, "/spec/payloadProcessing"
        ):
            _wait_for_resources(deployment_name, GATEWAY_NAMESPACE, requests_patch)
            _assert_only_changed(GATEWAY_NAMESPACE, "payload-processing", deployment_name, before)

    def test_pre_processing_resources_override_applies_to_tenant_deployment_only(self, shared_test_tenants):
        """Given a non-default tenant's MaasTenantConfig,
        when spec.payloadPreProcessing.resources is patched,
        then payload-pre-processing-{tenant} converges and siblings do not change.
        """
        if not _tenant_config_supports("payloadPreProcessing"):
            _skip_with_followup(
                reason=(
                    "Installed MaasTenantConfig CRD does not declare spec.payloadPreProcessing (added in 3.6); "
                    "the API server prunes the patch, so the Deployment can never converge."
                ),
                follow_up=CRD_FIELD_FOLLOW_UP,
            )

        tenant = _non_default_tenant(shared_test_tenants)
        deployment_name = per_tenant_ipp_names(tenant["name"])["pre_processing_deployment"]
        before = _snapshot_deployments(GATEWAY_NAMESPACE, "payload-pre-processing")
        assert deployment_name in before, f"Deployment/{deployment_name} not found; found {sorted(before)}"

        requests_patch = {"requests": IPP_RESOURCE_REQUESTS}
        with _tenant_config_patched(
            tenant["namespace"],
            {"payloadPreProcessing": {"resources": requests_patch}},
            "/spec/payloadPreProcessing",
        ):
            _wait_for_resources(deployment_name, GATEWAY_NAMESPACE, requests_patch)
            _assert_only_changed(GATEWAY_NAMESPACE, "payload-pre-processing", deployment_name, before)


@pytest.fixture(scope="module")
def tenant_key_governance(shared_test_tenants):
    """Give both shared tenants a model, MaaSAuthPolicy and MaaSSubscription.

    Minting an API key through a tenant's own maas-api needs that tenant's gateway
    AuthPolicy programmed, which maas-controller only reconciles once a tenant
    MaaSAuthPolicy exists; without it every mint returns 500 AUTH_FAILURE. Mirrors
    test_tenant_auth_isolation.py, which is the proven path for tenant-scoped keys.
    """
    suffix = uuid.uuid4().hex[:6]
    policy_name = f"e2e-mt-tenantcfg-{suffix}"
    subscription_name = f"e2e-mt-tenantcfg-{suffix}"
    cases = [dict(case) for case in shared_test_tenants]

    for case in cases:
        case["model_name"] = f"tenantcfg-model-{case['suffix']}"
        provision_tenant_model(case["model_name"], case["tenant_ns"], case["gateway_name"])
    try:
        for case in cases:
            make_tenant_model_accessible(
                case["model_name"],
                case["tenant_ns"],
                policy_name,
                subscription_name,
                gateway_name=case["gateway_name"],
                trlp_timeout=MODEL_BACKEND_READY_TIMEOUT,
            )
        yield subscription_name
    finally:
        _delete_governance_and_wait(
            subscriptions=[(subscription_name, case["tenant_ns"]) for case in cases],
            auth_policies=[(policy_name, case["tenant_ns"]) for case in cases],
        )
        for case in cases:
            _delete_cr("maasmodelref", case["model_name"], case["tenant_ns"])
            _delete_cr("llminferenceservice", case["model_name"], case["tenant_ns"])


@pytest.mark.api_keys
class TestNonDefaultTenantAPIKeysPolicy:
    """spec.apiKeys policy is enforced by the tenant's own maas-api and stays tenant-scoped."""

    def test_key_exceeding_tenant_policy_is_rejected(self, shared_test_tenants, tenant_key_governance):
        """Given spec.apiKeys.maxExpirationDays on a non-default MaasTenantConfig,
        when a key is minted through that tenant's maas-api with a longer expiry,
        then the mint is rejected.
        """
        tenant = _non_default_tenant(shared_test_tenants)
        token = _get_cluster_token()
        with _tenant_config_patched(
            tenant["namespace"], {"apiKeys": {"maxExpirationDays": TENANT_MAX_EXPIRATION_DAYS}}, "/spec/apiKeys"
        ):
            response = create_api_key_at(
                tenant["base_url"],
                token,
                f"e2e-mt-policy-exceeds-{uuid.uuid4().hex[:6]}",
                subscription=tenant_key_governance,
                expires_in=f"{EXCEEDS_TENANT_WITHIN_DEFAULT_DAYS * HOURS_PER_DAY}h",
            )
            assert response.status_code == 400, (
                f"Expected 400 minting a {EXCEEDS_TENANT_WITHIN_DEFAULT_DAYS}-day key against a "
                f"{TENANT_MAX_EXPIRATION_DAYS}-day tenant policy, got {response.status_code}"
            )

    def test_policy_does_not_apply_to_sibling_tenant(self, shared_test_tenants, tenant_key_governance):
        """Given spec.apiKeys.maxExpirationDays set on one non-default tenant,
        when the same over-limit key is minted through a sibling tenant's maas-api,
        then it succeeds, showing the policy is namespace-scoped.
        """
        sibling, tenant = shared_test_tenants[0], _non_default_tenant(shared_test_tenants)
        token = _get_cluster_token()
        expires_in = f"{EXCEEDS_TENANT_WITHIN_DEFAULT_DAYS * HOURS_PER_DAY}h"
        with _tenant_config_patched(
            tenant["namespace"], {"apiKeys": {"maxExpirationDays": TENANT_MAX_EXPIRATION_DAYS}}, "/spec/apiKeys"
        ):
            response = create_api_key_at(
                sibling["base_url"],
                token,
                f"e2e-mt-policy-sibling-{uuid.uuid4().hex[:6]}",
                subscription=tenant_key_governance,
                expires_in=expires_in,
            )
            assert response.status_code in (200, 201), (
                f"Sibling tenant should not inherit the {TENANT_MAX_EXPIRATION_DAYS}-day policy; "
                f"minting a {EXCEEDS_TENANT_WITHIN_DEFAULT_DAYS}-day key returned {response.status_code}"
            )


class TestNonDefaultTenantTelemetry:
    """spec.telemetry drives the tenant's own usage-log wiring."""

    def test_usage_logs_envoyfilter_is_tenant_scoped(self, shared_test_tenants):
        """Given usage logging enabled for a non-default tenant,
        when its usage-logs EnvoyFilter is rendered,
        then it is named for that tenant and attributes usage to that tenant's namespace.
        """
        tenant = _non_default_tenant(shared_test_tenants)
        filter_name = f"{USAGE_LOGS_ENVOY_FILTER_BASE}-{tenant['name']}"
        envoy_filter: dict[str, Any] | None = _get_cr("envoyfilter", filter_name, GATEWAY_NAMESPACE)
        if not envoy_filter:
            _skip_with_followup(
                reason=(
                    f"EnvoyFilter/{filter_name} is not present in '{GATEWAY_NAMESPACE}'. Usage-log wiring is "
                    f"only rendered when usage logging is enabled cluster-wide and Istio EnvoyFilters exist."
                ),
                follow_up=CRD_FIELD_FOLLOW_UP,
            )

        serialized = json.dumps(envoy_filter)
        assert tenant["namespace"] in serialized, (
            f"EnvoyFilter/{filter_name} should attribute usage to tenant namespace "
            f"'{tenant['namespace']}'; not found in its config"
        )
        assert tenant["gateway_name"] in serialized, (
            f"EnvoyFilter/{filter_name} should select Gateway '{tenant['gateway_name']}'; "
            f"otherwise usage logs come from the wrong workload"
        )

    def test_logs_capture_user_applies_to_tenant_filter_only(self, shared_test_tenants):
        """Given usage logging enabled for a non-default tenant,
        when spec.telemetry.logs.captureUser is patched on its MaasTenantConfig,
        then its usage-logs EnvoyFilter gains user_id wiring and a sibling's does not.

        captureUser carries a GDPR warning in the controller, so leakage onto another
        tenant's filter would be a privacy defect rather than a cosmetic one.
        """
        if not _tenant_config_supports("telemetry", "logs", "captureUser"):
            _skip_with_followup(
                reason=(
                    "Installed MaasTenantConfig CRD does not declare spec.telemetry.logs.captureUser; "
                    "the API server prunes the patch, so the EnvoyFilter can never gain user_id wiring."
                ),
                follow_up=CRD_FIELD_FOLLOW_UP,
            )

        tenant, sibling = _non_default_tenant(shared_test_tenants), shared_test_tenants[0]
        filter_name = f"{USAGE_LOGS_ENVOY_FILTER_BASE}-{tenant['name']}"
        sibling_filter_name = f"{USAGE_LOGS_ENVOY_FILTER_BASE}-{sibling['name']}"
        if not _get_cr("envoyfilter", filter_name, GATEWAY_NAMESPACE):
            _skip_with_followup(
                reason=f"EnvoyFilter/{filter_name} is not present in '{GATEWAY_NAMESPACE}'.",
                follow_up=CRD_FIELD_FOLLOW_UP,
            )

        def _captures_user_id(name: str) -> bool:
            obj = _get_cr("envoyfilter", name, GATEWAY_NAMESPACE)
            return bool(obj) and '"user_id"' in json.dumps(obj)

        assert not _captures_user_id(filter_name), (
            f"EnvoyFilter/{filter_name} already carries user_id before captureUser is set; "
            f"the test cannot distinguish the patch from the starting state"
        )
        sibling_before = _captures_user_id(sibling_filter_name)

        with _tenant_config_patched(
            tenant["namespace"], {"telemetry": {"logs": {"captureUser": True}}}, "/spec/telemetry"
        ):
            wait_for_json(
                "envoyfilter",
                filter_name,
                GATEWAY_NAMESPACE,
                predicate=lambda obj: '"user_id"' in json.dumps(obj),
                timeout=CONVERGENCE_TIMEOUT,
            )
            assert _captures_user_id(sibling_filter_name) == sibling_before, (
                f"EnvoyFilter/{sibling_filter_name} user_id wiring changed while only "
                f"{tenant['name']} enabled telemetry.logs.captureUser; user identity must not leak "
                f"into another tenant's usage logs"
            )
