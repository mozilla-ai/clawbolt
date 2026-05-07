"""AppFolio Vendor Portal integration package.

Self-contained integration: API client (``service``), magic-link auth
(``auth``), Pydantic models (``params``), and tool builders. The
``factory`` module registers with the tool registry at import time.

Auth model: passwordless magic link. The user receives an email from
AppFolio with ``?magic_link_token=...``; we exchange that for a Bearer
JWT against ``vendor.appf.io/access`` and persist it. There is no
refresh token; when the JWT expires the user requests a new magic link
and we re-exchange.
"""
