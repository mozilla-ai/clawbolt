"""Unit tests for backend.app.version env-var precedence and OSS_REF reads.

These exercise the helpers behind ``GET /api/admin/version`` directly so a
refactor of the env precedence or the file-fallback chain cannot regress
silently. The endpoint-level tests live in ``test_admin_router.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.app import version as version_module


@pytest.fixture(autouse=True)
def _clear_version_cache() -> Iterator[None]:
    """Reset the lru_caches around each test so env changes take effect."""
    version_module._premium_commit.cache_clear()
    version_module._oss_commit.cache_clear()
    version_module._oss_version.cache_clear()
    version_module._premium_version.cache_clear()
    yield
    version_module._premium_commit.cache_clear()
    version_module._oss_commit.cache_clear()
    version_module._oss_version.cache_clear()
    version_module._premium_version.cache_clear()


class TestPremiumCommit:
    def test_explicit_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``CLAWBOLT_PREMIUM_COMMIT`` is the highest-priority source."""
        monkeypatch.setenv("CLAWBOLT_PREMIUM_COMMIT", "explicit-sha")
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "railway-sha")
        assert version_module._premium_commit() == "explicit-sha"

    def test_railway_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without the explicit override, Railway's auto-injected SHA is used."""
        monkeypatch.delenv("CLAWBOLT_PREMIUM_COMMIT", raising=False)
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "railway-sha")
        assert version_module._premium_commit() == "railway-sha"

    def test_unknown_when_no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAWBOLT_PREMIUM_COMMIT", raising=False)
        monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
        assert version_module._premium_commit() == "unknown"

    def test_empty_env_treated_as_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty strings are skipped so the Dockerfile's empty default does not win."""
        monkeypatch.setenv("CLAWBOLT_PREMIUM_COMMIT", "")
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "railway-sha")
        assert version_module._premium_commit() == "railway-sha"

    def test_value_is_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAWBOLT_PREMIUM_COMMIT", "  sha-with-whitespace\n")
        assert version_module._premium_commit() == "sha-with-whitespace"


class TestOSSCommit:
    def test_env_override_wins_over_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``CLAWBOLT_OSS_COMMIT`` short-circuits the file lookup."""
        monkeypatch.setenv("CLAWBOLT_OSS_COMMIT", "env-oss-sha")
        # Even if the file says something else, env wins.
        monkeypatch.setattr(
            version_module,
            "_OSS_REF_CANDIDATES",
            (tmp_path / "OSS_REF",),
        )
        (tmp_path / "OSS_REF").write_text("file-oss-sha\n")
        assert version_module._oss_commit() == "env-oss-sha"

    def test_reads_first_existing_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("CLAWBOLT_OSS_COMMIT", raising=False)
        missing = tmp_path / "missing-OSS_REF"
        present = tmp_path / "OSS_REF"
        present.write_text("file-oss-sha\n")
        monkeypatch.setattr(
            version_module,
            "_OSS_REF_CANDIDATES",
            (missing, present),
        )
        assert version_module._oss_commit() == "file-oss-sha"

    def test_unknown_when_no_file_and_no_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("CLAWBOLT_OSS_COMMIT", raising=False)
        monkeypatch.setattr(
            version_module,
            "_OSS_REF_CANDIDATES",
            (tmp_path / "missing",),
        )
        assert version_module._oss_commit() == "unknown"


class TestOSSVersion:
    def test_env_override_wins_over_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CLAWBOLT_OSS_VERSION", "v9.9.9")
        monkeypatch.setattr(
            version_module,
            "_OSS_VERSION_CANDIDATES",
            (tmp_path / "OSS_VERSION",),
        )
        (tmp_path / "OSS_VERSION").write_text("v0.0.1\n")
        assert version_module._oss_version() == "v9.9.9"

    def test_reads_first_existing_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("CLAWBOLT_OSS_VERSION", raising=False)
        missing = tmp_path / "missing-OSS_VERSION"
        present = tmp_path / "OSS_VERSION"
        present.write_text("v0.4.4\n")
        monkeypatch.setattr(
            version_module,
            "_OSS_VERSION_CANDIDATES",
            (missing, present),
        )
        assert version_module._oss_version() == "v0.4.4"

    def test_empty_when_no_file_and_no_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Untagged OSS bumps are a normal state; the UI hides the version
        # slot when the field is empty, rather than rendering "unknown".
        monkeypatch.delenv("CLAWBOLT_OSS_VERSION", raising=False)
        monkeypatch.setattr(
            version_module,
            "_OSS_VERSION_CANDIDATES",
            (tmp_path / "missing",),
        )
        assert version_module._oss_version() == ""

    def test_empty_file_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # An OSS_REF bump to an untagged main commit writes an empty
        # OSS_VERSION file; the helper must treat that as "no version".
        monkeypatch.delenv("CLAWBOLT_OSS_VERSION", raising=False)
        empty_file = tmp_path / "OSS_VERSION"
        empty_file.write_text("\n")
        monkeypatch.setattr(
            version_module,
            "_OSS_VERSION_CANDIDATES",
            (empty_file,),
        )
        assert version_module._oss_version() == ""

    def test_empty_env_string_is_explicit_clear(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Setting the env var to "" deliberately clears the version (e.g.
        # to debug an untagged build) and must not fall through to the file.
        monkeypatch.setenv("CLAWBOLT_OSS_VERSION", "")
        present = tmp_path / "OSS_VERSION"
        present.write_text("v0.4.4\n")
        monkeypatch.setattr(
            version_module,
            "_OSS_VERSION_CANDIDATES",
            (present,),
        )
        assert version_module._oss_version() == ""

    def test_value_is_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAWBOLT_OSS_VERSION", "  v0.4.4\n")
        assert version_module._oss_version() == "v0.4.4"


class TestVersionInfoShape:
    def test_get_version_info_returns_all_fields(self) -> None:
        info = version_module.get_version_info()
        assert set(info.keys()) == {
            "premium_version",
            "premium_commit",
            "oss_version",
            "oss_commit",
            "started_at",
        }
        for value in info.values():
            assert isinstance(value, str)
