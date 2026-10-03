# E2E Test Fixtures

This directory contains kustomizations for end-to-end testing that combine public samples with test-only fixtures.

## Contents

### Public Samples (from `docs/samples/maas-system/`)
- **free**: `system:authenticated` group, 100 tokens/min (served ID `facebook/opt-125m`)
- **premium**: `premium-user` group, 1000 tokens/min (served ID `facebook/opt-125m-premium`)

### Test-Only Fixtures
- **unconfigured**: Model with no MaaSAuthPolicy or MaaSSubscription (served ID `test/e2e-unconfigured-model`; validates gateway denies access with 403)
- **distinct**: First distinct model serving `test/e2e-distinct-model` (validates multiple distinct models in subscriptions)
- **distinct-2**: Second distinct model serving `test/e2e-distinct-model-2` (validates multiple distinct models in subscriptions)
- **trlp-test**: Model used to validate TRLP validation behavior
- **embedding**: Embedding model (validates `/v1/embeddings`, TRLP, default-deny)
- **echo-sim**: Simulator running `--mode echo` serving `test/e2e-echo-model` (validates large mirrored I/O and credential leaks)

#### About echo-sim

Every other simulator fixture runs `--mode random`, which synthesizes a response
from `max_tokens` and a histogram. The content is unrelated to the prompt and its
size is not something a test can set, so two things cannot be validated:

- **Large I/O** — in `echo` mode the response mirrors the request, so response size
  is controlled by prompt size. That makes body limits, buffering and streaming
  behavior testable deterministically.
- **Leaks** — a canary string placed in the prompt comes back verbatim, so a test
  can assert exactly where it does and does not appear (response, logs, metrics).

`echo-sim` is additive. It ships its own LLMIS with a distinct served ID, so the
shared `random` simulators used by every other test are untouched.

Like `distinct` and `distinct-2`, the fixture ships only an LLMIS and a
MaaSModelRef — tests create their own MaaSSubscription and MaaSAuthPolicy.

> [!NOTE]
> An overlay adding `--log-http` is deliberately **not** included. That flag makes
> the simulator log full HTTP requests and responses, including `Authorization`
> headers and API keys, so it writes secrets to CI logs by design. It needs team
> agreement before it goes anywhere near shared CI.

## Usage

### For E2E Tests (CI)

```bash
# Deploy all fixtures (public samples + test-only)
kustomize build test/e2e/fixtures | kubectl apply -f -
```

### For Manual Testing

To deploy only the public samples without test fixtures, use:

```bash
# Public samples only (free + premium)
kustomize build docs/samples/maas-system | kubectl apply -f -
```

## Note

⚠️ **Do not use this kustomization for production or sample installations.** It includes test-only models that are designed to validate edge cases and should not be deployed in normal usage scenarios. For sample installations, use `docs/samples/maas-system/` instead.
