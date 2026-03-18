"""Export the OpenAPI spec from the OSS FastAPI app to a JSON file.

Usage:
    uv run python scripts/export_openapi.py [output_path]

Default output: frontend/openapi.json
"""

import json
import sys

from backend.app.main import app

spec = app.openapi()
out = sys.argv[1] if len(sys.argv) > 1 else "frontend/openapi.json"

with open(out, "w") as f:
    json.dump(spec, f, indent=2)
    f.write("\n")

print(f"Wrote OpenAPI spec to {out} ({len(spec.get('paths', {}))} paths)")
