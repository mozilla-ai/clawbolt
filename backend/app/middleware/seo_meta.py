"""SEO meta tag injection for marketing pages.

Intercepts HTML responses for marketing routes and injects route-specific
title, description, and Open Graph tags into the served index.html.
"""

from __future__ import annotations

from dataclasses import dataclass

from starlette.types import ASGIApp, Message, Receive, Scope, Send


@dataclass(frozen=True)
class PageMeta:
    title: str
    description: str
    og_type: str = "website"
    og_image: str = ""


ROUTE_META: dict[str, PageMeta] = {
    "/": PageMeta(
        title="Clawbolt",
        description="AI-powered assistant for contractors and tradespeople.",
    ),
}


def inject_meta(html: str, meta: PageMeta) -> str:
    """Replace placeholder meta tags in index.html with route-specific values."""
    tags = (
        f"<title>{meta.title}</title>\n"
        f'    <meta name="description" content="{meta.description}" />\n'
        f'    <meta property="og:title" content="{meta.title}" />\n'
        f'    <meta property="og:description" content="{meta.description}" />\n'
        f'    <meta property="og:type" content="{meta.og_type}" />'
    )
    if meta.og_image:
        tags += f'\n    <meta property="og:image" content="{meta.og_image}" />'

    # Replace existing <title>...</title> if present
    import re

    html = re.sub(r"<title>[^<]*</title>", "", html, count=1)
    # Inject before </head>
    html = html.replace("</head>", f"    {tags}\n  </head>", 1)
    return html


class SeoMetaMiddleware:
    """Inject route-specific meta tags into HTML responses for marketing pages.

    Pure ASGI middleware (no BaseHTTPMiddleware) to avoid streaming issues.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        meta = ROUTE_META.get(path)

        if meta is None:
            await self.app(scope, receive, send)
            return

        # Buffer the response to modify HTML content
        response_started = False
        original_headers: list[tuple[bytes, bytes]] = []
        body_parts: list[bytes] = []
        status_code = 200

        async def buffering_send(message: Message) -> None:
            nonlocal response_started, status_code
            if message["type"] == "http.response.start":
                response_started = True
                original_headers.extend(message.get("headers", []))
                status_code = message.get("status", 200)
            elif message["type"] == "http.response.body":
                body_parts.append(message.get("body", b""))

        await self.app(scope, receive, buffering_send)

        # Check if this is an HTML response
        content_type = ""
        for name, value in original_headers:
            if name.lower() == b"content-type":
                content_type = value.decode("utf-8", errors="replace")
                break

        body = b"".join(body_parts)

        if "text/html" in content_type:
            html = body.decode("utf-8", errors="replace")
            html = inject_meta(html, meta)
            body = html.encode("utf-8")

        # Update content-length header
        new_headers = []
        for name, value in original_headers:
            if name.lower() == b"content-length":
                new_headers.append((name, str(len(body)).encode()))
            else:
                new_headers.append((name, value))

        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": new_headers,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
            }
        )
