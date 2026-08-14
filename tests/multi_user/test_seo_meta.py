"""Tests for SEO meta tag injection middleware."""

from backend.app.middleware.seo_meta import PageMeta, inject_meta


class TestInjectMeta:
    def test_injects_title_and_og_tags(self) -> None:
        html = "<html><head><title>App</title></head><body></body></html>"
        meta = PageMeta(title="Home | Clawbolt", description="AI assistant.")
        result = inject_meta(html, meta)
        assert "<title>Home | Clawbolt</title>" in result
        assert 'name="description" content="AI assistant."' in result
        assert 'property="og:title" content="Home | Clawbolt"' in result
        assert 'property="og:description" content="AI assistant."' in result
        assert 'property="og:type" content="website"' in result
        # Original title tag should be replaced
        assert "<title>App</title>" not in result

    def test_injects_og_image_when_present(self) -> None:
        html = "<html><head></head><body></body></html>"
        meta = PageMeta(
            title="Pricing",
            description="Plans.",
            og_image="https://example.com/og.png",
        )
        result = inject_meta(html, meta)
        assert 'property="og:image" content="https://example.com/og.png"' in result

    def test_no_og_image_when_empty(self) -> None:
        html = "<html><head></head><body></body></html>"
        meta = PageMeta(title="About", description="About us.")
        result = inject_meta(html, meta)
        assert "og:image" not in result
