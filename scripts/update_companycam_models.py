#!/usr/bin/env python3
"""Download the CompanyCam OpenAPI spec and regenerate Pydantic models.

Usage:
    uv run python scripts/update_companycam_models.py

Requires: datamodel-code-generator (dev dependency)
"""

import subprocess
import sys
from pathlib import Path

SPEC_URL = "https://raw.githubusercontent.com/CompanyCam/openapi-spec/main/openapi.yaml"
SPEC_PATH = Path("backend/app/integrations/companycam/companycam_openapi.yaml")
MODELS_PATH = Path("backend/app/integrations/companycam/models.py")


def main() -> None:
    import httpx

    print(f"Downloading OpenAPI spec from {SPEC_URL}...")
    resp = httpx.get(SPEC_URL, follow_redirects=True, timeout=30)
    resp.raise_for_status()
    SPEC_PATH.write_bytes(resp.content)
    print(f"Saved to {SPEC_PATH} ({len(resp.content)} bytes)")

    print("Generating Pydantic models...")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "datamodel_code_generator",
            "--input",
            str(SPEC_PATH),
            "--output",
            str(MODELS_PATH),
            "--output-model-type",
            "pydantic_v2.BaseModel",
            "--target-python-version",
            "3.11",
            "--use-standard-collections",
            "--use-union-operator",
            "--field-constraints",
            "--snake-case-field",
            "--capitalise-enum-members",
            "--enum-field-as-literal",
            "all",
            "--use-annotated",
            "--collapse-root-models",
            "--use-double-quotes",
        ],
        check=True,
    )

    # Fix known issues: smart quotes from CompanyCam's spec descriptions
    content = MODELS_PATH.read_text(encoding="utf-8")
    content = content.replace("\u2019", "'")
    MODELS_PATH.write_text(content, encoding="utf-8")

    print(f"Generated {MODELS_PATH}")
    print(f"Run `ruff format {MODELS_PATH}` to finalize.")


if __name__ == "__main__":
    main()
