"""Export the OpenAPI spec to a JSON file.

The spec is exported in ``multi_user`` mode on purpose, so it documents
every route the product can serve. The frontend ships as one bundle for
both modes and calls those routes through a typed client, so a spec
narrowed to ``single_user`` would leave the sign-in, account, and admin
calls untyped and fail ``npm run typecheck``.

A ``single_user`` deployment simply does not mount the extra paths; the
frontend gates on ``/api/auth/config`` at runtime rather than on the
shape of this file.

Usage:
    uv run python scripts/export_openapi.py [output_path]

Default output: frontend/openapi.json
"""

import json
import os
import sys

# Must precede the import: Settings reads the environment once, and
# create_app() decides what to mount from the value it sees.
os.environ.setdefault("AUTH_MODE", "multi_user")

from backend.app.main import app

spec = app.openapi()
out = sys.argv[1] if len(sys.argv) > 1 else "frontend/openapi.json"

with open(out, "w") as f:
    json.dump(spec, f, indent=2)
    f.write("\n")

print(f"Wrote OpenAPI spec to {out} ({len(spec.get('paths', {}))} paths)")
