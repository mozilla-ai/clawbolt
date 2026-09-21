"""Request and response models for every HTTP surface.

Split by surface rather than by verb, so a handler imports the one module
that matches the page it serves: ``schemas.admin`` for the admin console,
``schemas.channels`` for channel setup, ``schemas.shared_data`` for the
consent-gated content views, and so on. ``schemas.common`` holds the few
primitives every surface needs.

There is deliberately no re-export here. Importing from the owning module
keeps each caller's dependencies visible in its import block.
"""
