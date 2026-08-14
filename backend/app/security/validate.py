"""Startup validation for the KMS configuration.

Run during pre-deploy. If ``KMS_KEY_ARN`` isn't set, this is a no-op
(the deployment runs on ``LocalKEKProvider`` and there's nothing to
validate). When configured, it does a single Encrypt/Decrypt round
trip against the configured key with a known plaintext sentinel; any
failure - missing IAM permissions, deleted key, wrong region, expired
credentials - aborts the deploy before traffic switches over.

Invocation:

    python -m backend.app.security.validate

Exit code is 0 on success or when KMS isn't configured. Non-zero on
any KMS error, with the exception logged for the deploy console.
"""

from __future__ import annotations

import logging
import sys

from backend.app.config import settings
from backend.app.security.kms import KMSEnvelopeKEKProvider

logger = logging.getLogger(__name__)

_SENTINEL_PLAINTEXT = b"clawbolt-kms-sentinel-v1"


def validate_kms() -> None:
    """Round-trip a sentinel through KMS to confirm access.

    Raises:
        Any boto3/botocore exception if KMS is configured but unreachable.
    """
    if not settings.kms_key_arn:
        logger.info(
            "KMS_KEY_ARN not set; skipping KMS validation. "
            "Encrypted columns will use LocalKEKProvider."
        )
        return

    provider = KMSEnvelopeKEKProvider(
        kms_key_arn=settings.kms_key_arn,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )
    kek_id, wrapped = provider.wrap(_SENTINEL_PLAINTEXT, context={})
    recovered = provider.unwrap(kek_id, wrapped, context={})
    if recovered != _SENTINEL_PLAINTEXT:
        raise RuntimeError(
            "KMS round-trip mismatch: unwrap(wrap(sentinel)) != sentinel. "
            "Cannot start: KMS is configured but produces inconsistent output."
        )
    logger.info(
        "KMS validation successful (key=%s, region=%s)",
        settings.kms_key_arn,
        provider.region,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        validate_kms()
    except Exception:
        logger.exception("KMS validation failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
