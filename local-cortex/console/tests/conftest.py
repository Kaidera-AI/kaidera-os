import pytest


class FakeCortex:
    """Records the lifecycle calls run_one makes; scriptable claim result."""
    def __init__(self, claim_ok=True):
        self.claim_ok = claim_ok
        self.calls = []          # list[tuple[str, dict]]

    async def claim_handoff(self, handoff_id, agent):
        self.calls.append(("claim", {"handoff_id": handoff_id, "agent": agent}))
        return self.claim_ok

    async def log(self, agent, event_type, summary, project=None):
        self.calls.append(("log", {"agent": agent, "event_type": event_type, "summary": summary}))

    async def complete_handoff(self, handoff_id):
        self.calls.append(("complete", {"handoff_id": handoff_id}))


class FakeRunner:
    """Yields a scripted harness event stream from stream_chat()."""
    def __init__(self, events):
        self._events = events
        self.last_call = None

    async def stream_chat(
        self,
        message,
        *,
        model=None,
        system=None,
        harness=None,
        reasoning=None,
        workspace=None,
        project_key=None,
        run_context=None,
    ):
        # Record the call so tests can assert the worker forwards routing (incl. the
        # reasoning level — it used to be silently dropped before reaching the harness).
        self.last_call = {
            "message": message, "model": model, "system": system,
            "harness": harness, "reasoning": reasoning,
            "workspace": workspace, "project_key": project_key,
            "run_context": run_context,
        }
        for ev in self._events:
            yield ev


@pytest.fixture
def fake_cortex():
    return FakeCortex()


@pytest.fixture
def routing_stub():
    return lambda agent, project: ("pi", "gpt-5.3-codex-spark", "high")


@pytest.fixture
def ed25519_public_license(monkeypatch):
    """Issue an Ed25519-signed license + wire the app's verify key — the REAL platform
    grant path. The PUBLIC edition rejects forgeable HMAC (only platform-signed Ed25519
    grants verify), so any public-edition test that expects a grant to UNLOCK something
    must sign with Ed25519. Returns a callable `issue(customer, **generate_license_kwargs)`."""
    from app import license as lic
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    def _issue(customer, **kw):
        priv = Ed25519PrivateKey.generate()
        pub_pem = priv.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode("ascii")
        monkeypatch.setenv("KAIDERA_OS_LICENSE_VERIFY_KEY", pub_pem)
        tok = lic.generate_license(customer, alg="ed25519", ed25519_private_key=priv, **kw)
        monkeypatch.setenv("KAIDERA_OS_LICENSE_KEY", tok)
        return tok

    return _issue
