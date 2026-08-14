"""Tests for KMSEnvelopeKEKProvider and the dormant plugin wiring.

KMS calls are stubbed via ``unittest.mock`` so CI doesn't need real
AWS credentials. The unit under test is the wrapper around boto3, not
boto3 itself.
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest

from backend.app.security.dek_cache import DEKCache
from backend.app.security.encryption import KEKProvider, LocalKEKProvider
from backend.app.security.kms import KMS_KEK_ID, KMSEnvelopeKEKProvider

_TEST_ARN = "arn:aws:kms:us-east-1:123456789012:key/abcdef-1234-5678-90ab-cdef12345678"


@pytest.fixture()
def reset_plugin_kek_cache() -> Generator[None]:
    """Reset the plugin's cached KEK provider before and after each test.

    Without teardown, a test that mutates ``plugin._kek_provider`` and
    then fails an assertion leaves the singleton populated for the
    next test, causing surprise pass/fail order dependencies.
    """
    from backend.app.auth import loader as plugin_module

    plugin_module._kek_provider = None
    yield
    plugin_module._kek_provider = None


def _fake_kms_client() -> MagicMock:
    """Build a MagicMock that round-trips encrypt/decrypt deterministically.

    Encrypts by appending a marker; decrypts by stripping it. Lets us
    assert "what KMS got" and "what came back" without modeling real
    KMS semantics.
    """
    client = MagicMock(name="kms_client")
    marker = b"|wrapped|"

    def _encrypt(*, KeyId: str, Plaintext: bytes) -> dict[str, bytes]:
        del KeyId  # not asserted here
        return {"CiphertextBlob": marker + Plaintext}

    def _decrypt(*, CiphertextBlob: bytes) -> dict[str, bytes]:
        if not CiphertextBlob.startswith(marker):
            raise RuntimeError("fake KMS: malformed ciphertext")
        return {"Plaintext": CiphertextBlob[len(marker) :]}

    client.encrypt.side_effect = _encrypt
    client.decrypt.side_effect = _decrypt
    return client


def test_constructor_rejects_empty_arn() -> None:
    with pytest.raises(ValueError, match="kms_key_arn must be a non-empty string"):
        KMSEnvelopeKEKProvider(kms_key_arn="")
    with pytest.raises(ValueError, match="kms_key_arn must be a non-empty string"):
        KMSEnvelopeKEKProvider(kms_key_arn="   ")


def test_constructor_extracts_region_from_arn() -> None:
    provider = KMSEnvelopeKEKProvider(kms_key_arn=_TEST_ARN)
    assert provider._aws_region == "us-east-1"


def test_constructor_rejects_malformed_arn() -> None:
    with pytest.raises(ValueError, match="Invalid KMS ARN"):
        KMSEnvelopeKEKProvider(kms_key_arn="not-an-arn")


def test_wrap_round_trips_through_kms() -> None:
    """wrap() returns kek_id='kms' and a CiphertextBlob from KMS."""
    fake_client = _fake_kms_client()
    with patch.object(KMSEnvelopeKEKProvider, "_kms_client", return_value=fake_client):
        provider = KMSEnvelopeKEKProvider(kms_key_arn=_TEST_ARN)
        kek_id, wrapped = provider.wrap(b"my-dek-bytes-32-long-padding-ok!", context={})
    assert kek_id == KMS_KEK_ID
    assert wrapped == b"|wrapped|my-dek-bytes-32-long-padding-ok!"
    fake_client.encrypt.assert_called_once_with(
        KeyId=_TEST_ARN, Plaintext=b"my-dek-bytes-32-long-padding-ok!"
    )


def test_unwrap_decrypts_via_kms_when_kek_id_matches() -> None:
    fake_client = _fake_kms_client()
    with patch.object(KMSEnvelopeKEKProvider, "_kms_client", return_value=fake_client):
        provider = KMSEnvelopeKEKProvider(kms_key_arn=_TEST_ARN, cache=DEKCache())
        kek_id, wrapped = provider.wrap(b"the-dek", context={})
        recovered = provider.unwrap(kek_id, wrapped, context={})
    assert recovered == b"the-dek"


def test_unwrap_falls_through_to_local_provider_for_legacy_rows() -> None:
    """Rows written before KMS rolled out carry kek_id='local'. The
    composite provider routes them to its fallback, not KMS."""
    fake_client = _fake_kms_client()
    fallback = LocalKEKProvider()
    legacy_dek = b"X" * 44
    legacy_kek_id, legacy_wrapped = fallback.wrap(legacy_dek, context={})
    assert legacy_kek_id == LocalKEKProvider.KEK_ID

    with patch.object(KMSEnvelopeKEKProvider, "_kms_client", return_value=fake_client):
        provider = KMSEnvelopeKEKProvider(kms_key_arn=_TEST_ARN, fallback_provider=fallback)
        recovered = provider.unwrap(legacy_kek_id, legacy_wrapped, context={})
    assert recovered == legacy_dek
    # KMS was never called for the legacy unwrap.
    fake_client.decrypt.assert_not_called()


def test_unwrap_uses_cache_to_skip_kms_on_repeat_reads() -> None:
    """Same wrapped DEK -> cache hit -> exactly one KMS call across N reads."""
    fake_client = _fake_kms_client()
    cache = DEKCache(ttl_seconds=60)
    with patch.object(KMSEnvelopeKEKProvider, "_kms_client", return_value=fake_client):
        provider = KMSEnvelopeKEKProvider(kms_key_arn=_TEST_ARN, cache=cache)
        kek_id, wrapped = provider.wrap(b"dek", context={})
        for _ in range(5):
            assert provider.unwrap(kek_id, wrapped, context={}) == b"dek"
    assert fake_client.decrypt.call_count == 1


# ---------------------------------------------------------------------------
# Loader wiring (dormant when KMS_KEY_ARN unset)
# ---------------------------------------------------------------------------


def test_loader_returns_local_provider_when_kms_unset(
    monkeypatch: pytest.MonkeyPatch,
    reset_plugin_kek_cache: None,
) -> None:
    """Empty KMS_KEY_ARN leaves DEK wrapping on the local provider.

    This is the dormant-by-default state, and the one every deployment
    that has not provisioned a key is running.
    """
    from backend.app.auth import loader
    from backend.app.config import settings
    from backend.app.security.encryption import LocalKEKProvider

    monkeypatch.setattr(settings, "kms_key_arn", "")

    assert isinstance(loader.get_kek_provider(), LocalKEKProvider)


def test_loader_returns_kms_provider_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    reset_plugin_kek_cache: None,
) -> None:
    """Setting KMS_KEY_ARN switches wrapping to KMS on the next start."""
    from backend.app.auth import loader
    from backend.app.config import settings

    monkeypatch.setattr(settings, "kms_key_arn", _TEST_ARN)
    monkeypatch.setattr(settings, "aws_access_key_id", "AKIA-TEST")
    monkeypatch.setattr(settings, "aws_secret_access_key", "secret")

    provider = loader.get_kek_provider()
    assert isinstance(provider, KMSEnvelopeKEKProvider)

    # Subsequent calls return the same instance (singleton).
    assert loader.get_kek_provider() is provider


def test_loader_ignores_whitespace_only_arn(
    monkeypatch: pytest.MonkeyPatch,
    reset_plugin_kek_cache: None,
) -> None:
    """``KMS_KEY_ARN=" "`` is a typo, not a request for KMS.

    The field validator strips it to empty, so resolution falls back
    rather than handing a blank ARN to the provider constructor.
    """
    from backend.app.auth import loader
    from backend.app.config import Settings
    from backend.app.security.encryption import LocalKEKProvider

    monkeypatch.setattr(loader.settings, "kms_key_arn", Settings(KMS_KEY_ARN="   ").kms_key_arn)

    assert isinstance(loader.get_kek_provider(), LocalKEKProvider)


def test_kms_provider_satisfies_kek_provider_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structural type check: the provider must satisfy KEKProvider."""
    fake_client = _fake_kms_client()
    with patch.object(KMSEnvelopeKEKProvider, "_kms_client", return_value=fake_client):
        provider = KMSEnvelopeKEKProvider(kms_key_arn=_TEST_ARN)

    def _accepts_kek_provider(p: KEKProvider) -> KEKProvider:
        return p

    assert _accepts_kek_provider(provider) is provider


def test_wrap_propagates_kms_errors() -> None:
    """When boto3 raises (missing creds, deleted key, network failure),
    ``wrap`` propagates the exception rather than swallowing it.

    Silent fallback would be a security regression: callers expect
    KMS-wrapped output and would otherwise persist mis-encrypted data.
    """
    fake_client = MagicMock(name="kms_client_failing")
    fake_client.encrypt.side_effect = RuntimeError("simulated KMS failure")

    with patch.object(KMSEnvelopeKEKProvider, "_kms_client", return_value=fake_client):
        provider = KMSEnvelopeKEKProvider(kms_key_arn=_TEST_ARN)
        with pytest.raises(RuntimeError, match="simulated KMS failure"):
            provider.wrap(b"dek-bytes", context={})


def test_region_property_exposes_arn_region() -> None:
    """``region`` property is the read-only public access for log/diagnostic
    surfaces (e.g. validate.py) so callers don't reach into _aws_region."""
    provider = KMSEnvelopeKEKProvider(kms_key_arn=_TEST_ARN)
    assert provider.region == "us-east-1"
