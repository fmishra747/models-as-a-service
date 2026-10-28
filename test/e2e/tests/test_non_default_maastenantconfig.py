"""MaasTenantConfig apiKeys settings on a non-default tenant (RHOAIENG-100759).

Test that spec.apiKeys policy is enforced at a tenant's own maas-api without
leaking to sibling tenants. Field-landing checks (maasApi, payloadProcessing,
telemetry) are verified in operands_int_test.go (envtest) where they're stable
and don't require xdist coordination.
"""

from __future__ import annotations

import contextlib
import json
import re
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import requests
from multitenancy_helpers import (
    GATEWAY_NAMESPACE,
    INFRA_NAMESPACE,
    MODEL_BACKEND_READY_TIMEOUT,
    TENANT_CONFIG_KIND,
    TENANT_CR_NAME,
    _oc_run,
    bearer_headers,
    create_api_key_at,
    make_tenant_model_accessible,
    per_tenant_ipp_names,
    per_tenant_maas_api_names,
    provision_tenant_model,
    wait_for_json,
)
from test_helper import (
    TIMEOUT,
    TLS_VERIFY,
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


def _wait_for_env_rollout(name: str, namespace: str, env_name: str, expected: str) -> None:
    """Wait until a Deployment exposes env_name=expected and has finished rolling out.

    MaasTenantConfig api-key policy does not reach maas-api directly: the controller
    resolves spec.apiKeys into the maas-parameters ConfigMap, which is substituted
    into the Deployment env, and maas-api only reads it at startup. So the value is
    not in force until a new pod is serving.
    """

    def _matches(obj: dict[str, Any]) -> bool:
        spec, status = obj.get("spec") or {}, obj.get("status") or {}
        containers = ((spec.get("template") or {}).get("spec") or {}).get("containers") or []
        if not containers:
            return False
        env = {e.get("name"): e.get("value") for e in containers[0].get("env") or []}
        if env.get(env_name) != expected:
            return False
        desired = spec.get("replicas", 1)
        # status.replicas must also equal desired: with maxSurge the new pod can be
        # counted in updatedReplicas while the OLD pod is still the available one and
        # still serving. Requiring no surge pods left is what proves the rollout is done.
        return (
            status.get("observedGeneration", 0) >= (obj.get("metadata") or {}).get("generation", 0)
            and status.get("replicas") == desired
            and status.get("updatedReplicas") == desired
            and status.get("availableReplicas") == desired
            and status.get("readyReplicas") == desired
        )

    wait_for_json("deployment", name, namespace, predicate=_matches, timeout=CONVERGENCE_TIMEOUT)

def _effective_max_expiration_days(base_url: str, token: str) -> int | None:
    """Ask a maas-api instance what expiration cap it is actually applying.

    GET /v1/api-keys/config reports GetMaxExpirationDays(), the same value the
    create path enforces against. Querying it separates "the policy never reached
    the serving pod" from "it arrived and was not enforced", which the mint status
    code alone cannot distinguish.
    """
    response = requests.get(
        f"{base_url}/v1/api-keys/config",
        headers=bearer_headers(token),
        timeout=TIMEOUT,
        verify=TLS_VERIFY,
    )
    if response.status_code != 200:
        return None
    return response.json().get("max_expiration_days")

def _non_default_tenant(shared_test_tenants) -> dict[str, str]:
    """Return the tenant to patch (tenant B), leaving tenant A as the untouched sibling."""
    return shared_test_tenants[1]


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
            _wait_for_env_rollout(
                per_tenant_maas_api_names(tenant["name"])["deployment"],
                INFRA_NAMESPACE,
                "API_KEY_MAX_EXPIRATION_DAYS",
                str(TENANT_MAX_EXPIRATION_DAYS),
            )
            # Pinpoint which hop failed if this test goes red: the Deployment env and
            # rollout are already confirmed above, so a stale value here means the
            # policy never reached the serving pod rather than not being enforced.
            effective = _effective_max_expiration_days(tenant["base_url"], token)
            assert effective == TENANT_MAX_EXPIRATION_DAYS, (
                f"maas-api for {tenant['name']} reports max_expiration_days={effective!r} although its "
                f"Deployment env is {TENANT_MAX_EXPIRATION_DAYS} and the rollout completed; "
                f"spec.apiKeys.maxExpirationDays did not reach the serving pod"
            )
            response = create_api_key_at(
                tenant["base_url"],
                token,
                f"e2e-mt-policy-exceeds-{uuid.uuid4().hex[:6]}",
                subscription=tenant_key_governance,
                expires_in=f"{EXCEEDS_TENANT_WITHIN_DEFAULT_DAYS * HOURS_PER_DAY}h",
            )
            assert response.status_code == 400, (
                f"maas-api reports max_expiration_days={effective}, yet minting a "
                f"{EXCEEDS_TENANT_WITHIN_DEFAULT_DAYS}-day key returned {response.status_code} "
                f"instead of 400 — the cap is known to the service but not enforced on create"
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
            # Assert isolation only once the patched tenant's policy is actually in force.
            _wait_for_env_rollout(
                per_tenant_maas_api_names(tenant["name"])["deployment"],
                INFRA_NAMESPACE,
                "API_KEY_MAX_EXPIRATION_DAYS",
                str(TENANT_MAX_EXPIRATION_DAYS),
            )
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


