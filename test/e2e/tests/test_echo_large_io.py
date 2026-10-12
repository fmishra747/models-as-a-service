"""Large-I/O and credential-leak tests against the echo-mode simulator.

The other simulator fixtures run ``--mode random``: the response is synthesized
from ``max_tokens`` and a histogram, so its content is unrelated to the prompt
and its size is not something a test can set. ``echo-simulator`` runs
``--mode echo``, which mirrors the last user message back verbatim. That makes
the response a deterministic function of the request, which is what these two
classes rely on:

* **Large I/O** — response size follows prompt size, so a test can push a known
  payload through the gateway and assert it survives the round trip intact.
  Anything that truncates or mutates the body (buffer limits, ext_proc, wasm)
  shows up as a length or content mismatch. Under ``--mode random`` a corrupted
  request still yields a perfectly normal-looking response, so these faults are
  invisible.
* **Leaks** — a canary placed in the prompt comes back verbatim, so a test can
  assert exactly where it does and does not appear.

Behaviour of ``llm-d-inference-sim:v0.8.2`` that shaped these tests:

* ``max_tokens`` does **not** truncate the echo; a small cap still returns the
  whole payload.
* Reported ``usage`` counts whitespace-delimited words rather than bytes, so
  these tests never assert on token counts. Use the ``random`` simulators for
  token accounting.
* The simulator rejects requests above its context window with a 400. The
  fixture sets ``--max-model-len 131072``; ``LARGE_WORDS`` stays well inside it.
"""

import logging
import uuid

import pytest
import requests
from test_helper import (
    ECHO_MODEL_NAME,
    ECHO_MODEL_PATH,
    ECHO_MODEL_REF,
    MODEL_NAMESPACE,
    TIMEOUT,
    TLS_VERIFY,
    _create_api_key,
    _create_sa_token,
    _create_test_auth_policy,
    _create_test_subscription,
    _delete_governance_and_wait,
    _gateway_url,
    _ns,
    _sa_to_user,
    _wait_for_gateway_auth_enforced,
    _wait_for_model_ready,
)

log = logging.getLogger(__name__)

# Own xdist group so these do not lengthen an existing one, and serial because
# creating the MaaSAuthPolicy regenerates the shared gateway AuthPolicy. If the
# second pass gets long, this could move to worker_tenant isolation instead.
pytestmark = [pytest.mark.xdist_group("echo_io"), pytest.mark.serial]

# ~20k words / ~108KB. Comfortably inside the fixture's 131072-token context
# window while still being far larger than any default buffer the request
# crosses on its way through the gateway.
LARGE_WORDS = 20000

# The module sends a handful of LARGE_WORDS round trips, each billing ~40k
# tokens (prompt plus mirrored completion). Ten million per minute keeps rate
# limiting out of the way without relying on the newer unlimited field.
TOKEN_BUDGET = 10_000_000


def _lorem(words: int) -> str:
    """Whitespace-delimited filler, so the simulator's tokenizer sees real words."""
    return (("lorem ipsum dolor sit amet " * ((words // 5) + 1)).strip()).rsplit(" ", 1)[0]


def _echo_completion(api_key: str, prompt: str, max_tokens: int = 16) -> requests.Response:
    """POST a chat completion to the echo model.

    Chat rather than text completions: echo mode mirrors the last *user message*,
    which only exists on the chat endpoint.
    """
    return requests.post(
        f"{_gateway_url()}{ECHO_MODEL_PATH}/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": ECHO_MODEL_NAME,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        },
        timeout=TIMEOUT,
        verify=TLS_VERIFY,
    )


def _content(response: requests.Response) -> str:
    return response.json()["choices"][0]["message"]["content"]


@pytest.fixture(scope="module")
def echo_access():
    """Grant a service account access to the echo model and mint it an API key.

    The echo-sim fixture ships only an LLMIS and a MaaSModelRef, matching the
    distinct fixtures, so the subscription and auth policy are created here.
    """
    ns = _ns()
    sa_name = "e2e-echo-sa"
    auth_name = "e2e-echo-auth"
    subscription_name = "e2e-echo-sub"

    oc_token = _create_sa_token(sa_name, namespace=MODEL_NAMESPACE)
    sa_user = _sa_to_user(sa_name, namespace=MODEL_NAMESPACE)

    # Everything from the first CR onward sits inside the try: if setup fails
    # partway -- an invalid subscription, a pairing that never goes Ready -- the
    # yield never happens, so without this the already-created auth policy would
    # leak onto the cluster and the next run would inherit it.
    try:
        _create_test_auth_policy(auth_name, ECHO_MODEL_REF, users=[sa_user])
        # A large echo bills roughly one token per whitespace-delimited word in
        # both directions, so LARGE_WORDS costs ~40k tokens per request and the
        # default budget would turn these into 429s before the body assertions
        # ever run. An explicit ceiling rather than unlimited=True: the unlimited
        # field is a newer addition and clusters whose CRD predates it reject the
        # subscription with "spec.modelRefs[0].tokenRateLimits: Required value".
        _create_test_subscription(
            subscription_name,
            ECHO_MODEL_REF,
            users=[sa_user],
            token_limit=TOKEN_BUDGET,
            window="1m",
        )

        # The MaaSModelRef ships Pending: it holds at
        # "GovernanceAttached=False NoPairingFound" until a subscription and auth
        # policy pair exists, and only then publishes its endpoint. Without this
        # wait the first request can race the route and fail as a 404 or 403 that
        # looks like a product bug rather than a fixture that is not up yet.
        _wait_for_model_ready(ECHO_MODEL_REF, namespace=MODEL_NAMESPACE, timeout=120)
        _wait_for_gateway_auth_enforced()

        yield _create_api_key(oc_token, subscription=subscription_name)
    finally:
        _delete_governance_and_wait(
            subscriptions=[(subscription_name, ns)],
            auth_policies=[(auth_name, ns)],
        )


@pytest.mark.models
class TestEchoFidelity:
    """The echo contract these tests depend on."""

    def test_response_mirrors_prompt(self, echo_access):
        """A small prompt comes back verbatim, proving the fixture is in echo mode."""
        prompt = f"echo-fidelity-{uuid.uuid4()}"
        r = _echo_completion(echo_access, prompt)
        assert r.status_code == 200, r.text
        assert _content(r) == prompt

    def test_max_tokens_does_not_truncate(self, echo_access):
        """max_tokens must not clip the echo, or large-I/O assertions are meaningless."""
        prompt = _lorem(500)
        r = _echo_completion(echo_access, prompt, max_tokens=1)
        assert r.status_code == 200, r.text
        assert _content(r) == prompt


@pytest.mark.models
class TestLargeIO:
    """Large payloads must survive the round trip through the gateway intact."""

    def test_large_prompt_round_trips_unmodified(self, echo_access):
        """~108KB through gateway, ext_proc and wasm must come back byte-identical."""
        prompt = _lorem(LARGE_WORDS)
        log.info(f"Sending {len(prompt)} byte prompt to {ECHO_MODEL_PATH}")

        r = _echo_completion(echo_access, prompt)
        assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"

        content = _content(r)
        assert len(content) == len(prompt), (
            f"response truncated or padded: sent {len(prompt)} bytes, got {len(content)}"
        )
        assert content == prompt, "response body was mutated in transit"

    def test_response_size_tracks_prompt_size(self, echo_access):
        """Growing the prompt must grow the response, with no ceiling in between.

        A buffer limit on the path would show up as the larger response being
        clamped while the smaller one passes.
        """
        small = _lorem(1000)
        large = _lorem(LARGE_WORDS)

        r_small = _echo_completion(echo_access, small)
        assert r_small.status_code == 200, r_small.text
        r_large = _echo_completion(echo_access, large)
        assert r_large.status_code == 200, r_large.text

        assert len(_content(r_small)) == len(small)
        assert len(_content(r_large)) == len(large)
        assert len(_content(r_large)) > len(_content(r_small))


@pytest.mark.security
class TestCredentialLeak:
    """The API key must not be reflected back to the caller."""

    def test_api_key_not_echoed_in_response(self, echo_access):
        """Echo mirrors the user message; it must never mirror the credential.

        If the gateway ever folded auth headers into the forwarded body, the
        simulator would echo them straight back to the caller.
        """
        prompt = f"leak-probe-{uuid.uuid4()}"
        r = _echo_completion(echo_access, prompt)
        assert r.status_code == 200, r.text

        body = r.text
        assert echo_access not in body, "API key reflected in the response body"
        assert "Bearer " not in body, "Authorization header reflected in the response body"

    def test_canary_in_prompt_is_not_leaked_to_headers(self, echo_access):
        """A canary in the body must stay in the body, not surface in response headers."""
        canary = f"CANARY-{uuid.uuid4()}"
        r = _echo_completion(echo_access, canary)
        assert r.status_code == 200, r.text

        assert _content(r) == canary, "echo did not return the canary"
        leaked = [k for k, v in r.headers.items() if canary in v]
        assert not leaked, f"canary leaked into response headers: {leaked}"
