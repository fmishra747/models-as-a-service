# MaaS E2E Testing

**Ownership:** Deep MaaS behavior is tested here (controller, CRDs, gateway policies, maas-api). DSC toggling MaaS, `ModelsAsServiceReady`, Tenant presence/absence vs DSC, and thin operator smoke belong in the operator repo.

## Quick start

Full deploy and pytest (same path CI uses):

```bash
./test/e2e/scripts/prow_run_smoke_test.sh
```

Existing cluster (skip deploy):

```bash
SKIP_DEPLOYMENT=true ./test/e2e/scripts/prow_run_smoke_test.sh
```

Parallel pytest on an existing cluster (default 7 workers):

```bash
SKIP_DEPLOYMENT=true ./test/e2e/run-tests-quick.sh
```

Smoke helper only:

```bash
./test/e2e/smoke.sh
```

## Local prerequisites

- OpenShift access (`oc` logged in)
- From repo root: `cd test/e2e`, create venv, `pip install -r requirements.txt`
- Most HTTP tests need `GATEWAY_HOST` (and often routes/API reachable). Full env list: `tests/test_helper.py` docstring.

Three env vars are easy to miss when running by hand rather than through
`prow_run_smoke_test.sh`, and each fails in a way that does not name the cause:

```bash
# Required. The Gateway Route is maas-gateway-route; the Gateway CR is maas-default-gateway.
export GATEWAY_HOST=$(oc get route maas-gateway-route -n openshift-ingress -o jsonpath='{.spec.host}')

# RHOAI clusters. Defaults to "opendatahub", from which every infra namespace is derived;
# on RHOAI that resolves to odh-ai-gateway-infra instead of redhat-ai-gateway-infra, and
# tenant bootstrap then waits out its timeout on an HTTPRoute that is never created.
export DEPLOYMENT_NAMESPACE=redhat-ods-applications

# Per-tenant Gateway Routes are self-signed. CI supplies a CA bundle at /tmp/ca-bundle.crt;
# locally there is none, so tenant-scoped HTTP calls fail TLS verification.
export E2E_SKIP_TLS_VERIFY=true
```

Check `DEPLOYMENT_NAMESPACE` against the cluster rather than its name — an
`odh-*` cluster may still run the controller in `redhat-ods-applications`:

```bash
oc get deploy -A | grep -E 'maas-controller|maas-api'
```

## Pytest modules

```bash
cd test/e2e && source .venv/bin/activate   # after setup above
pytest tests/<file>.py -v
```

| File | Focus |
|------|--------|
| `test_smoke.py` | Read-only smoke checks: non-streaming and streaming (`text/event-stream`) chat/completions |
| `test_subscription.py` | Subscription / inference flows |
| `test_trlp_rate_grouping.py` | TRLP limits grouped by rate: shared limits, per-subscription and per-user budgets, rate edits, gateway wasm config size guard |
| `test_api_keys.py` | `/v1/api-keys` |
| `test_models_endpoint.py` | `/v1/models` |
| `test_negative_security.py` | Security / negative paths on the **default** gateway (`GATEWAY_HOST`) |
| `test_namespace_scoping.py` | Namespace wiring |
| `test_external_models.py` | External model refs |
| `test_tenant.py` | `default-tenant` (subscription namespace): Ready/phase, optional payload-processing (gateway namespace), user CRs not owned by Tenant |
| `test_aitenant_lifecycle.py` | `AITenant` bootstrap create/delete; reserved namespace rejection; **serial:** revocation operand recovery when `maas-api-cleanup` SA is removed mid-delete |
| `test_tenant_namespace_discovery.py` | Multi-tenant namespace discovery (S1), webhooks (S6); smoke enables `ENABLE_TENANT_NAMESPACE_DISCOVERY=true` by default |
| `test_gateway_scoped_authpolicy.py` | Gateway-scoped `maas-gateway-auth` (S10 / #912); runs in default CI |
| `test_multi_tenant_integration.py` | Multi-tenant lifecycle and coexistence scenarios; smoke enables tenant namespace discovery by default |
| `test_tenant_model_inference.py` | Per-tenant inference, symmetric cross-gateway API-key isolation, AuthPolicy EnvoyFilter workload selectors (RHOAIENG-99372), and per-tenant gateway negative auth (RHOAIENG-100758; `TestTenantGatewayNegativeSecurity`) |
| `test_multi_tenant_maas_api.py` | Per-tenant `maas-api` infrastructure (S24); gated by `ENABLE_S24_E2E=true` |
| `test_tenant_auth_isolation.py` | Tenant-scoped API-key/OIDC isolation (S4); gated by `ENABLE_S4_E2E=true` and tenant API URLs |
| `test_tenant_subscription_isolation.py` | Tenant-scoped subscription listing/selection (S4); gated by `ENABLE_S4_E2E=true` and tenant API URLs |
| `test_tenant_rate_limit_isolation.py` | Tenant-scoped rate-limit isolation (S4); gated by `ENABLE_S4_E2E=true` and tenant API URLs |
| `test_config_tenant.py` | Cluster `Config/default`: anchor present, owner refs on Tenant and `maas-controller` Deployment (skips if Config CRD missing) |
| `test_non_default_maastenantconfig.py` | `MaasTenantConfig` settings on a **non-default** tenant (RHOAIENG-100759): `maasApi`, `payloadProcessing`/`payloadPreProcessing`, `apiKeys`, `telemetry`, each asserted to stay confined to the patched tenant |

The shared CI runner and `smoke.sh` execute all tests under `tests/`. Individual modules can still be run directly, for example `pytest tests/test_subscription_list_endpoints.py -v`.

**Skips:** `test_tenant.py` and `test_config_tenant.py` skip the whole module when the needed CRD or object is absent (partial cluster or older bundle). Neither module deletes Config or exercises DSC disable; that stays in operator or manual teardown.

### MaasTenantConfig coverage: default vs non-default tenants

`MaasTenantConfig` is a namespace-scoped singleton. Its name is always
`default-tenant`, pinned by a CEL rule — the **namespace** is what scopes it to a
tenant. So "the non-default tenant's config" is still `MaasTenantConfig/default-tenant`,
living in that tenant's namespace, and it drives resources suffixed with the
tenant name (`maas-api-{tenant}`, `payload-processing-{tenant}`).

| `spec` field | Default tenant | Non-default tenant |
|---|---|---|
| `maasApi.replicas`, `.resources` | `test_tenant.py` | `test_non_default_maastenantconfig.py` |
| `payloadProcessing.resources` | `test_tenant.py` | `test_non_default_maastenantconfig.py` |
| `payloadPreProcessing.resources` | `test_tenant.py` | `test_non_default_maastenantconfig.py` |
| `apiKeys.maxExpirationDays` | `test_api_keys.py` (platform default, config not patched) | `test_non_default_maastenantconfig.py` (policy patched, then enforced via that tenant's own maas-api) |
| `telemetry` | `test_aitenant_lifecycle.py` — spec values persist on the CR | `test_non_default_maastenantconfig.py` — usage-log EnvoyFilter wiring matches the spec |
| `guardrails` | none | none |

Two conventions the non-default tests rely on, worth keeping if you extend them:

- **Isolation is proved by comparison, not assumption.** Every prefix-matching
  Deployment in the namespace is snapshotted before the patch, and only the
  patched tenant's is allowed to differ afterwards. A single named comparison
  would miss an override leaking onto a third tenant.
- **A field missing from the installed CRD is skipped, not failed.** The API
  server prunes unknown fields, so the patch appears to apply and the resource
  never converges — indistinguishable from a product bug unless the schema is
  checked first. Skips must name a follow-up issue so they do not become
  permanent; `test_tenant.py` follows the same pattern for `spec.maasApi`.

## CI

CI runs `./test/e2e/scripts/prow_run_smoke_test.sh`, a thin orchestrator that sequences standalone scripts:

| Phase | Script | Responsibility |
|-------|--------|----------------|
| 1. Deploy platform | `scripts/deploy-platform.sh` | cert-manager, ODH, deploy.sh, OIDC/Keycloak |
| 2. Deploy models | `scripts/deploy-models.sh` | e2e fixtures (LLMIS, MaaSModelRef, AuthPolicy, Subscription) |
| 3. Setup tokens | `scripts/setup-test-tokens.sh` | admin + regular user tokens (htpasswd / SA fallback) |
| 4. Validate | `scripts/validate-deployment.sh` | pods, CRDs, deployment readiness |
| 5. Pre-test waits | `prow_run_smoke_test.sh` | gateway reachability, auth chain, OIDC readiness |
| 6. Test | `scripts/run_e2e_tests.sh` | pytest: two-pass xdist (parallel + serial) |

`prow_run_smoke_test.sh` contains only orchestration (prereqs, env defaults, phase sequencing, artifact collection). `deploy-platform.sh` and `deploy-models.sh` are runnable standalone (they bootstrap `PROJECT_ROOT` and source helpers if not already loaded). `setup-test-tokens.sh` must be sourced (exports `TOKEN`/`ADMIN_OC_TOKEN` into the parent shell).

`run_e2e_tests.sh` can also be called directly on an existing cluster (env vars must be exported) or via `run-tests-quick.sh` which sets up the env and delegates to the same runner.

Deployment uses **Red Hat Connectivity Link (RHCL)** from the cluster `redhat-operators` catalog (`POLICY_ENGINE=rhcl` by default, channel head unless `RHCL_STARTING_CSV` is set) into **`kuadrant-system`**. Reports are written to `ARTIFACT_DIR` when set.

Multi-tenancy discovery tests run by default in `prow_run_smoke_test.sh`, which sets `ENABLE_TENANT_NAMESPACE_DISCOVERY=true` unless explicitly overridden and patches maas-controller before pytest. If set to `false`, `test_tenant_namespace_discovery.py` and `test_multi_tenant_integration.py` skip. When discovery is enabled, `test_namespace_scoping.py` skips (dormant-mode assumptions).

The dormant-mode regression inside `test_tenant_namespace_discovery.py` mutates controller flags and only runs when `ENABLE_TENANT_DISCOVERY_DORMANT_E2E=true`.

The S24/S4 suites are in the smoke list but intentionally skip until their backing implementation is present in the deployed build. Enable them with `ENABLE_S24_E2E=true` or `ENABLE_S4_E2E=true` plus `MAAS_API_BASE_URL_TENANT_A`, `MAAS_API_BASE_URL_TENANT_B`, `TENANT_A_NAMESPACE`, and `TENANT_B_NAMESPACE`.

External OIDC runs require `EXTERNAL_OIDC=true` and `OIDC_ISSUER_URL`, `OIDC_TOKEN_URL`, `OIDC_CLIENT_ID`, `OIDC_USERNAME`, `OIDC_PASSWORD` per your deploy/test setup.

## Nightly tier (`@pytest.mark.nightly`)

Tests marked `nightly` (for example `test_external_oidc.py` and per-tenant OIDC discovery) are **always excluded from passes 1–2**. They run only in **pass 3** (`-m nightly`) when `MAAS_PYTEST_INCLUDE_NIGHTLY=true` (set automatically when `EXTERNAL_OIDC=true` in prow, or explicitly on the ODH MaaS nightly in shepard). PR `/group-test` does not set that flag, so nightly tests are skipped there.

Local opt-in via the runner:

```bash
MAAS_PYTEST_INCLUDE_NIGHTLY=true EXTERNAL_OIDC=true \
  ./test/e2e/scripts/run_e2e_tests.sh
```

Or target nightly tests directly:

```bash
EXTERNAL_OIDC=true OIDC_ISSUER_URL=... OIDC_TOKEN_URL=... \
  pytest tests/ -m nightly -v
```

To run only the OIDC tests (pass 3) and skip passes 1–2, set `E2E_RUN_ONLY_OIDC_TESTS=true`. The dedicated OIDC CI job uses this because PR `/group-test` already covers passes 1–2. It requires `EXTERNAL_OIDC=true` and cannot be combined with `--serial-only`:

```bash
E2E_RUN_ONLY_OIDC_TESTS=true EXTERNAL_OIDC=true \
  ./test/e2e/scripts/run_e2e_tests.sh
```

Delegated Grid administration needs the strict OIDC contract on the default AITenant. When `EXTERNAL_OIDC=true`, `scripts/deploy.sh` also sets `audience`, `accessTokenPurposeClaim`, and `accessTokenPurpose` from `OIDC_AUDIENCE`, `OIDC_ACCESS_TOKEN_PURPOSE_CLAIM`, and `OIDC_ACCESS_TOKEN_PURPOSE` when they are provided. The test realm's tokens must carry matching `aud` and purpose claims.

## Parallel execution (pytest-xdist)

By default, `run_e2e_tests.sh` runs tests in **two marker-filtered passes** (default: 7 xdist workers on pass 1). `--serial-only` runs pass 2 (`-m "serial and not legacy_ipp"`) only and skips non-serial tests:

1. **Pass 1:** `-m "not serial and not legacy_ipp"` — parallel across files when `E2E_PARALLEL_WORKERS > 1` (`--dist=loadgroup`), or single-worker serial execution when `E2E_PARALLEL_WORKERS=1`
2. **Pass 2:** `-m "serial and not legacy_ipp"` — cluster-wide mutators (single worker): simulator-subscription lifecycle, UNCONFIGURED model auth, TRLP rebuilds, operator scale tests. Skipped when pass 1 fails (saves CI time on red runs).

Legacy maas-controller IPP suites (per-tenant IPP isolation, maas EnvoyFilter chain, ExternalModel reconciler) are marked `legacy_ipp` and skipped by default. Opt in with `pytest -m legacy_ipp` (from `test/e2e/` with env vars exported, or target specific files).

Pass 1 and pass 2 must stay separate: several modules mix `@serial` tests with worker-tenant tests, and module-scoped fixtures reject a single session that selects both.

For single-worker debugging (still two passes, no xdist):

```bash
E2E_PARALLEL_WORKERS=1 ./run-tests-quick.sh
```

Parallel on an existing cluster:

```bash
SKIP_DEPLOYMENT=true ./test/e2e/run-tests-quick.sh
```

| Variable | Default | Description |
|----------|---------|-------------|
| `E2E_PARALLEL_WORKERS` | `7` | Parallel workers for pass 1 (`-m "not serial and not legacy_ipp"`). Pass 2 (`@serial`, not `legacy_ipp`) always runs on one worker. Set to `1` for single-worker pass 1 (no xdist); marker split is unchanged. |
| `E2E_AUTHPOLICY_PHASE_TIMEOUT` | `120` (parallel) / `60` (serial) | MaaSAuthPolicy phase wait |
| `E2E_GATEWAY_ENFORCED_TIMEOUT` | `240` (parallel) / `180` (serial) | Kuadrant gateway auth enforced wait |
| `E2E_MULTITENANCY_PHASE_TIMEOUT` | `180` (parallel) / `120` (serial) | Tenant discovery phase wait |
| `E2E_SUBSCRIPTION_INFERENCE_READY_TIMEOUT` | `300` | Subscription discovery + direct/mirrored TRLP enforcement |
| `E2E_SUBSCRIPTION_TRLP_TIMEOUT` | `180` | Mirrored TRLP ready wait on tenant subscriptions |
| `E2E_MODEL_BACKEND_READY_TIMEOUT` | `300` | LLMInferenceService backend ready (tenant model provisioning) |
| `E2E_USE_WORKER_TENANT` | `true` | When `true`, xdist workers bootstrap a dedicated AITenant for Bucket C pilots (`test_subscription.py` first). Set `false` to keep using `models-as-a-service`. |

`@serial` tests run in pass 2; verify the current set with `pytest -m serial tests/ --collect-only -q`.

**Worker tenant (Phase 3 pilot):** each xdist worker (`gw0`, `gw1`, …) bootstraps its own AITenant namespace with baseline `simulator-subscription` / `simulator-access` CRs. Non-serial tests in opted-in modules route `MAAS_SUBSCRIPTION_NAMESPACE`, `GATEWAY_HOST`, and `MAAS_API_BASE_URL` to that tenant for the duration of the test.

Session fixtures suffix resource names with the worker id (for example `e2e-test-inference-key-w0`).
