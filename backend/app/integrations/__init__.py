"""Integration packages.

Each subdirectory is a self-contained integration (service layer, tool
builders, parameter models, and SKILL.md). The ``factory`` module in
each package calls ``_register()`` at import time to register with the
tool registry, following the same pattern as core tools.

Auto-discovery is handled by ``ensure_tool_modules_imported()`` in
``backend.app.agent.tools.registry``, which scans both the core
``tools/`` directory and this ``integrations/`` directory.
"""
