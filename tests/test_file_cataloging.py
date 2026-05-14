from unittest.mock import AsyncMock, patch

import pytest

from backend.app.agent.file_store import slugify as _slugify
from backend.app.agent.tools.file_tools import (
    DEFAULT_INBOX_FOLDER,
    _build_filename,
    _normalize_folder_path,
    create_file_tools,
)
from backend.app.agent.tools.names import ToolName
from backend.app.models import User
from tests.mocks.storage import MockStorageBackend


def test_slugify_basic() -> None:
    assert _slugify("Hello World") == "hello_world"


def test_slugify_special_chars() -> None:
    assert _slugify("A damaged deck railing!") == "a_damaged_deck_railing"


def test_slugify_max_length() -> None:
    result = _slugify("A very long description that exceeds the limit", max_length=15)
    assert len(result) <= 15


# ---------------------------------------------------------------------------
# _normalize_folder_path tests
# ---------------------------------------------------------------------------


def test_normalize_folder_path_defaults_to_inbox_when_blank() -> None:
    for raw in (None, "", "   "):
        normalized, error = _normalize_folder_path(raw)
        assert error is None
        assert normalized == DEFAULT_INBOX_FOLDER


def test_normalize_folder_path_accepts_client_style_nested_path() -> None:
    normalized, error = _normalize_folder_path("/Acme - 123 Main Street/photos")
    assert error is None
    assert normalized == "/Acme - 123 Main Street/photos"


def test_normalize_folder_path_strips_trailing_slash() -> None:
    normalized, _ = _normalize_folder_path("/Inbox/")
    assert normalized == "/Inbox"


def test_normalize_folder_path_accepts_root() -> None:
    normalized, error = _normalize_folder_path("/")
    assert error is None
    assert normalized == "/"


def test_normalize_folder_path_rejects_missing_leading_slash() -> None:
    normalized, error = _normalize_folder_path("Inbox")
    assert normalized is None
    assert error is not None
    assert "must start with '/'" in error


def test_normalize_folder_path_rejects_traversal() -> None:
    normalized, error = _normalize_folder_path("/Inbox/../Secrets")
    assert normalized is None
    assert error is not None
    assert "'.." in error or "'.'" in error


def test_normalize_folder_path_rejects_backslash() -> None:
    normalized, error = _normalize_folder_path(r"/Inbox\foo")
    assert normalized is None
    assert error is not None


def test_normalize_folder_path_rejects_empty_segment() -> None:
    normalized, error = _normalize_folder_path("/Inbox//foo")
    assert normalized is None
    assert error is not None


def test_normalize_folder_path_rejects_unsupported_chars() -> None:
    normalized, error = _normalize_folder_path("/Inbox/<weird>")
    assert normalized is None
    assert error is not None


def test_normalize_folder_path_accepts_unicode_segments() -> None:
    """Non-ASCII names like 'Müller Roofing' or 'Café Owners' must survive."""
    normalized, error = _normalize_folder_path("/Müller Roofing/photos")
    assert error is None
    assert normalized == "/Müller Roofing/photos"


# ---------------------------------------------------------------------------
# _build_filename tests
# ---------------------------------------------------------------------------


def test_build_filename_with_description() -> None:
    name = _build_filename("damaged railing", index=1, extension="jpg")
    assert name == "damaged_railing_001.jpg"


def test_build_filename_without_description() -> None:
    name = _build_filename("", index=2, extension="jpg")
    assert name == "file_002.jpg"


def test_build_filename_default_extension_is_bin() -> None:
    """The default extension is the safe-ish 'bin', not 'jpg'.

    Real callers in ``upload_to_storage`` always pass an explicit
    extension derived from the mime type. The default catches direct
    callers that forget; ``.bin`` is honest about the unknown.
    """
    name = _build_filename("note", index=1)
    assert name == "note_001.bin"


# ---------------------------------------------------------------------------
# upload_to_storage tool tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_upload_writes_file_to_caller_supplied_folder(
    test_user: User,
) -> None:
    """upload_to_storage should write to the folder_path the caller passed."""
    storage = MockStorageBackend()
    tools = create_file_tools(
        test_user,
        storage,
        pending_media={"https://example.com/media/photo.jpg": b"fake-image-bytes"},
    )
    upload = tools[0].function

    result = await upload(
        folder_path="/Johnson - 123 Main Streetreet/photos",
        description="Damaged deck railing",
        original_url="https://example.com/media/photo.jpg",
    )

    assert result.is_error is False
    assert result.content.startswith("ok")
    assert "damaged_deck_railing_001.jpg" in result.content
    assert any("Johnson - 123 Main Streetreet/photos" in key for key in storage.files)


@pytest.mark.asyncio()
async def test_upload_emits_receipt_with_drive_link(
    test_user: User,
) -> None:
    """Successful upload should attach a ToolReceipt carrying the Drive share URL.

    The receipt is what plain-text channels (iMessage, Telegram, SMS) and
    the webchat reply use to surface a tappable link without relying on
    the LLM to remember it across turns. Mirrors the CompanyCam upload
    pattern in ``companycam_upload_photo``.
    """
    storage = MockStorageBackend()
    tools = create_file_tools(
        test_user,
        storage,
        pending_media={"https://example.com/media/photo.jpg": b"img-bytes"},
    )
    upload = tools[0].function

    result = await upload(
        folder_path="/Inbox",
        description="reference shot",
        original_url="https://example.com/media/photo.jpg",
    )

    assert result.is_error is False
    assert result.receipt is not None
    assert result.receipt.action == "Uploaded file to Drive"
    assert result.receipt.target.startswith("/Inbox/")
    assert result.receipt.url is not None
    assert result.receipt.url.startswith("https://")


@pytest.mark.asyncio()
async def test_upload_persists_description_on_storage_metadata(
    test_user: User,
) -> None:
    """upload_to_storage should write description into the backend's metadata."""
    storage = MockStorageBackend()
    tools = create_file_tools(
        test_user,
        storage,
        pending_media={"https://example.com/p.jpg": b"img"},
    )
    upload = tools[0].function

    await upload(
        folder_path="/Loeffler/documents",
        description="receipt for fasteners",
        original_url="https://example.com/p.jpg",
    )

    saved = next(iter(storage.metadata.values()))
    assert saved.description == "receipt for fasteners"


@pytest.mark.asyncio()
async def test_upload_defaults_to_inbox_when_folder_path_omitted(
    test_user: User,
) -> None:
    """upload_to_storage should land files in /Inbox when no folder_path is provided."""
    storage = MockStorageBackend()
    tools = create_file_tools(
        test_user,
        storage,
        pending_media={"https://example.com/doc.pdf": b"pdf-bytes"},
    )
    upload = tools[0].function

    result = await upload(
        description="Invoice from supplier",
        mime_type="application/pdf",
    )

    assert result.is_error is False
    assert len(storage.files) == 1
    path = next(iter(storage.files))
    assert "Inbox/" in path
    assert path.endswith(".pdf")


@pytest.mark.asyncio()
async def test_upload_accepts_root_folder_path(
    test_user: User,
) -> None:
    """folder_path='/' should drop the file at the top of the user's Clawbolt folder."""
    storage = MockStorageBackend()
    tools = create_file_tools(
        test_user,
        storage,
        pending_media={"https://example.com/f.jpg": b"bytes"},
    )
    upload = tools[0].function

    result = await upload(folder_path="/", description="loose photo")
    assert result.is_error is False
    assert len(storage.files) == 1


@pytest.mark.asyncio()
async def test_upload_rejects_invalid_folder_path(
    test_user: User,
) -> None:
    """Path traversal and other malformed paths should error before touching storage."""
    storage = MockStorageBackend()
    tools = create_file_tools(
        test_user,
        storage,
        pending_media={"https://example.com/f.jpg": b"bytes"},
    )
    upload = tools[0].function

    result = await upload(folder_path="/Inbox/../etc", description="x")
    assert result.is_error is True
    assert "folder_path" in result.content
    assert len(storage.files) == 0


@pytest.mark.asyncio()
async def test_upload_no_media_returns_error_pointing_at_move_file(
    test_user: User,
) -> None:
    """Upload with no pending media should return an error pointing at move_file."""
    storage = MockStorageBackend()
    tools = create_file_tools(test_user, storage, pending_media={})
    upload = tools[0].function

    result = await upload(folder_path="/Inbox")
    assert "No file content" in result.content
    assert "move_file" in result.content
    assert result.is_error is True


@pytest.mark.asyncio()
async def test_upload_uses_first_media_if_no_url(
    test_user: User,
) -> None:
    """If no original_url specified, use first available media."""
    storage = MockStorageBackend()
    tools = create_file_tools(
        test_user,
        storage,
        pending_media={"https://example.com/media/auto.jpg": b"auto-bytes"},
    )
    upload = tools[0].function

    result = await upload(folder_path="/Inbox", description="Auto selected")
    assert result.content.startswith("ok")
    assert result.is_error is False
    assert len(storage.files) == 1


@pytest.mark.asyncio()
async def test_upload_sequential_indexing(
    test_user: User,
) -> None:
    """Multiple uploads to same folder should get sequential indices."""
    storage = MockStorageBackend()
    tools = create_file_tools(
        test_user,
        storage,
        pending_media={
            "https://example.com/1.jpg": b"img1",
            "https://example.com/2.jpg": b"img2",
        },
    )
    upload = tools[0].function

    result1 = await upload(
        folder_path="/Test Client/photos",
        original_url="https://example.com/1.jpg",
    )
    result2 = await upload(
        folder_path="/Test Client/photos",
        original_url="https://example.com/2.jpg",
    )

    assert "_001." in result1.content
    assert "_002." in result2.content


@pytest.mark.asyncio()
async def test_upload_creates_folder(
    test_user: User,
) -> None:
    """Storage folder should be created before upload."""
    storage = MockStorageBackend()
    tools = create_file_tools(
        test_user,
        storage,
        pending_media={"https://example.com/f.jpg": b"bytes"},
    )
    upload = tools[0].function

    await upload(folder_path="/Fence Client/photos")
    assert len(storage.folders) == 1
    assert "Fence Client" in storage.folders[0]
    assert "/photos" in storage.folders[0]


@pytest.mark.asyncio()
async def test_upload_picks_distinct_filenames_when_list_folder_is_stale(
    test_user: User,
) -> None:
    """Serial uploads to the same folder must not collide on the same index
    when ``list_folder`` returns stale results.

    Drive's ``files.list`` is eventually consistent: a file just written by
    ``upload_file`` may not appear in the next ``list_folder`` call. Before
    the per-turn ``recent_uploads_by_folder`` registry, three uploads to
    the same folder in one turn all read ``existing=[]`` and all minted
    ``photo_001.jpg``, silently shadowing one another in Drive (issue
    surfaced on nathan's 2026-05-13 ``/Catch All/photos/`` upload trio).
    """
    storage = MockStorageBackend()

    # Simulate the Drive eventual-consistency lag: ``list_folder`` always
    # returns whatever was visible at the time of the FIRST call this turn.
    # Subsequent uploads append to ``storage.files`` but the listing stays
    # frozen. Mirrors what Drive's search index does in production.
    stale_snapshot: list = []
    captured = {"first": False}
    real_list_folder = storage.list_folder

    async def stale_list_folder(path: str) -> list:
        if not captured["first"]:
            stale_snapshot[:] = await real_list_folder(path)
            captured["first"] = True
        return list(stale_snapshot)

    storage.list_folder = stale_list_folder  # type: ignore[method-assign]

    tools = create_file_tools(
        test_user,
        storage,
        pending_media={
            "https://example.com/a.jpg": b"aaa",
            "https://example.com/b.jpg": b"bbb",
            "https://example.com/c.jpg": b"ccc",
        },
    )
    upload = tools[0].function

    r1 = await upload(folder_path="/Catch All/photos", original_url="https://example.com/a.jpg")
    r2 = await upload(folder_path="/Catch All/photos", original_url="https://example.com/b.jpg")
    r3 = await upload(folder_path="/Catch All/photos", original_url="https://example.com/c.jpg")

    assert not (r1.is_error or r2.is_error or r3.is_error), (r1.content, r2.content, r3.content)
    written = sorted(k for k in storage.files if k.startswith("Catch All/photos/"))
    assert len(written) == 3
    # Three distinct sequence numbers, no duplicates.
    assert len({k for k in written}) == 3, f"duplicate filenames written: {written}"
    assert written == [
        "Catch All/photos/file_001.jpg",
        "Catch All/photos/file_002.jpg",
        "Catch All/photos/file_003.jpg",
    ]


# ---------------------------------------------------------------------------
# move_file tool tests
# ---------------------------------------------------------------------------


def _move_file_function(test_user: User, storage: MockStorageBackend):  # noqa: ANN202
    tools = create_file_tools(test_user, storage)
    return next(t for t in tools if t.name == ToolName.MOVE_FILE).function


@pytest.mark.asyncio()
async def test_move_file_relocates_to_named_folder(
    test_user: User,
) -> None:
    """move_file should move a saved file into the caller-supplied to_folder_path."""
    storage = MockStorageBackend()
    await storage.upload_file(b"img-data", "/Inbox", "file_001.jpg")

    move_file = _move_file_function(test_user, storage)

    result = await move_file(
        from_path="/Inbox/file_001.jpg",
        to_folder_path="/John Smith - 123 Main Streetreet/photos",
    )

    assert result.is_error is False
    assert result.content.startswith("ok")
    assert "John Smith - 123 Main Streetreet/photos/file_001.jpg" in result.content
    assert "Inbox/file_001.jpg" not in storage.files
    assert any("John Smith" in k for k in storage.files)


@pytest.mark.asyncio()
async def test_move_file_renames_when_filename_provided(
    test_user: User,
) -> None:
    """new_filename should override the source filename for the destination."""
    storage = MockStorageBackend()
    await storage.upload_file(b"img-data", "/Inbox", "file_001.jpg")

    move_file = _move_file_function(test_user, storage)

    result = await move_file(
        from_path="/Inbox/file_001.jpg",
        to_folder_path="/Acme/photos",
        new_filename="front_porch.jpg",
    )

    assert result.is_error is False
    assert "front_porch.jpg" in result.content


@pytest.mark.asyncio()
async def test_move_file_avoids_overwrite_on_filename_collision(
    test_user: User,
) -> None:
    """When the destination already has the target name, suffix with _002 etc."""
    storage = MockStorageBackend()
    await storage.upload_file(b"old", "/Inbox", "photo.jpg")
    await storage.upload_file(b"existing", "/Acme/photos", "photo.jpg")

    move_file = _move_file_function(test_user, storage)

    result = await move_file(
        from_path="/Inbox/photo.jpg",
        to_folder_path="/Acme/photos",
    )

    assert result.is_error is False
    assert "photo_002.jpg" in result.content


@pytest.mark.asyncio()
async def test_move_file_emits_receipt(
    test_user: User,
) -> None:
    """move_file should emit a ToolReceipt with the destination link."""
    storage = MockStorageBackend()
    await storage.upload_file(b"img-data", "/Inbox", "file_001.jpg")

    move_file = _move_file_function(test_user, storage)

    result = await move_file(
        from_path="/Inbox/file_001.jpg",
        to_folder_path="/Acme/photos",
    )

    assert result.is_error is False
    assert result.receipt is not None
    assert result.receipt.action == "Moved file in Drive"
    assert result.receipt.target.startswith("/Acme/photos/")
    assert result.receipt.url is not None


@pytest.mark.asyncio()
async def test_move_file_not_found(
    test_user: User,
) -> None:
    """move_file should return NOT_FOUND if the source path does not exist."""
    storage = MockStorageBackend()
    move_file = _move_file_function(test_user, storage)

    result = await move_file(
        from_path="/Inbox/nonexistent.jpg",
        to_folder_path="/Acme/photos",
    )
    assert result.is_error is True
    assert "File not found" in result.content


@pytest.mark.asyncio()
async def test_move_file_rejects_invalid_destination(
    test_user: User,
) -> None:
    """Path traversal in to_folder_path should be rejected before storage is touched."""
    storage = MockStorageBackend()
    await storage.upload_file(b"img-data", "/Inbox", "file_001.jpg")
    move_file = _move_file_function(test_user, storage)

    result = await move_file(
        from_path="/Inbox/file_001.jpg",
        to_folder_path="/../escape",
    )
    assert result.is_error is True


@pytest.mark.asyncio()
async def test_move_file_normalizes_missing_leading_slash(
    test_user: User,
) -> None:
    """from_path should be normalized when the LLM forgets the leading slash."""
    storage = MockStorageBackend()
    await storage.upload_file(b"img-data", "/Inbox", "file_002.jpg")
    move_file = _move_file_function(test_user, storage)

    result = await move_file(
        from_path="Inbox/file_002.jpg",
        to_folder_path="/Ralph Smith/photos",
    )
    assert result.is_error is False
    assert result.content.startswith("ok")


@pytest.mark.asyncio()
async def test_move_file_from_drive_root(
    test_user: User,
) -> None:
    """A file dropped at the Drive root should still be movable.

    Covers the ``old_folder == '/'`` branch in :func:`_split_file_path`;
    without that special case, ``rsplit('/', 1)`` would produce an empty
    ``old_folder`` and the underlying ``storage.move_file`` would
    receive a malformed source.
    """
    storage = MockStorageBackend()
    await storage.upload_file(b"img-data", "/", "stray.jpg")
    move_file = _move_file_function(test_user, storage)

    result = await move_file(
        from_path="/stray.jpg",
        to_folder_path="/Acme/photos",
    )

    assert result.is_error is False
    assert result.content.startswith("ok")
    assert any("Acme/photos/stray.jpg" in key for key in storage.files)


@pytest.mark.asyncio()
async def test_move_file_rejects_invalid_from_path(
    test_user: User,
) -> None:
    """Traversal or bad chars in from_path are caught before storage is touched."""
    storage = MockStorageBackend()
    await storage.upload_file(b"img-data", "/Inbox", "file_001.jpg")
    move_file = _move_file_function(test_user, storage)

    result = await move_file(
        from_path="/Inbox/../file_001.jpg",
        to_folder_path="/Acme/photos",
    )
    assert result.is_error is True
    assert "must not contain" in result.content or "from_path" in result.content


# ---------------------------------------------------------------------------
# durable retrieval tool tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_find_saved_files_matches_query_tokens(
    test_user: User,
) -> None:
    """find_saved_files should match against filenames and stored descriptions."""
    storage = MockStorageBackend()
    await storage.upload_file(
        b"img-data",
        "/Loeffler/documents",
        "receipt_001.jpg",
        mime_type="image/jpeg",
        description="receipt for fasteners",
    )
    await storage.upload_file(
        b"other",
        "/Acme/photos",
        "photo_001.jpg",
        mime_type="image/jpeg",
        description="front porch progress photo",
    )

    tools = create_file_tools(test_user, storage)
    find_saved = next(t for t in tools if t.name == ToolName.FIND_SAVED_FILES).function

    result = await find_saved(query="Loeffler receipt")

    assert result.is_error is False
    assert "/Loeffler/documents/receipt_001.jpg" in result.content
    assert "/Acme/photos/photo_001.jpg" not in result.content


@pytest.mark.asyncio()
@patch("backend.app.agent.tools.file_tools.run_vision_on_media", new_callable=AsyncMock)
async def test_analyze_saved_file_reads_from_durable_storage(
    mock_vision: AsyncMock,
    test_user: User,
) -> None:
    """analyze_saved_file should download bytes from storage and run vision."""
    mock_vision.return_value = "Receipt total: $29.91."

    storage = MockStorageBackend()
    await storage.upload_file(
        b"saved-image-bytes",
        "/Loeffler/documents",
        "receipt_001.jpg",
        mime_type="image/jpeg",
        description="receipt for fasteners",
    )

    tools = create_file_tools(test_user, storage)
    analyze_saved = next(t for t in tools if t.name == ToolName.ANALYZE_SAVED_FILE).function

    result = await analyze_saved(
        file_ref="/Loeffler/documents/receipt_001.jpg",
        context="Pull the total",
    )

    assert result.is_error is False
    assert result.content == "Receipt total: $29.91."
    mock_vision.assert_awaited_once_with(
        b"saved-image-bytes",
        "image/jpeg",
        "Pull the total",
    )


@pytest.mark.asyncio()
@patch("backend.app.agent.tools.file_tools.run_vision_on_media", new_callable=AsyncMock)
async def test_analyze_saved_file_uses_turn_text_when_context_omitted(
    mock_vision: AsyncMock,
    test_user: User,
) -> None:
    """analyze_saved_file should fall back to the current turn text like analyze_photo."""
    mock_vision.return_value = "The receipt total is $29.91."

    storage = MockStorageBackend()
    await storage.upload_file(
        b"saved-image-bytes",
        "/Loeffler/documents",
        "receipt_001.jpg",
        mime_type="image/jpeg",
        description="receipt for fasteners",
    )

    tools = create_file_tools(test_user, storage, turn_text="What was the total on this receipt?")
    analyze_saved = next(t for t in tools if t.name == ToolName.ANALYZE_SAVED_FILE).function

    result = await analyze_saved(file_ref="/Loeffler/documents/receipt_001.jpg")

    assert result.is_error is False
    assert result.content == "The receipt total is $29.91."
    mock_vision.assert_awaited_once_with(
        b"saved-image-bytes",
        "image/jpeg",
        "What was the total on this receipt?",
    )


@pytest.mark.asyncio()
async def test_analyze_saved_file_rejects_non_image(
    test_user: User,
) -> None:
    """analyze_saved_file should reject saved non-image documents."""
    storage = MockStorageBackend()
    await storage.upload_file(
        b"pdf-bytes",
        "/Loeffler/documents",
        "invoice_001.pdf",
        mime_type="application/pdf",
        description="supplier invoice",
    )

    tools = create_file_tools(test_user, storage)
    analyze_saved = next(t for t in tools if t.name == ToolName.ANALYZE_SAVED_FILE).function

    result = await analyze_saved(file_ref="/Loeffler/documents/invoice_001.pdf")

    assert result.is_error is True
    assert "not an image" in result.content
