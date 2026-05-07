from unittest.mock import AsyncMock, patch

import pytest

from backend.app.agent.file_store import slugify as _slugify
from backend.app.agent.tools.file_tools import (
    _build_client_folder,
    _build_filename,
    build_folder_path,
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
# _build_client_folder tests
# ---------------------------------------------------------------------------


def test_build_client_folder_both() -> None:
    assert _build_client_folder("John Smith", "116 Virginia Ave") == (
        "John Smith - 116 Virginia Ave"
    )


def test_build_client_folder_name_only() -> None:
    assert _build_client_folder("Jane Doe") == "Jane Doe"


def test_build_client_folder_address_only() -> None:
    assert _build_client_folder(client_address="42 Elm St") == "42 Elm St"


def test_build_client_folder_none() -> None:
    assert _build_client_folder() == ""


def test_build_client_folder_whitespace_only() -> None:
    assert _build_client_folder("  ", "  ") == ""


# ---------------------------------------------------------------------------
# build_folder_path tests
# ---------------------------------------------------------------------------


def test_build_folder_path_with_client_name_and_address() -> None:
    path = build_folder_path("job_photo", client_name="John", client_address="116 Virginia Ave")
    assert path == "/John - 116 Virginia Ave/photos"


def test_build_folder_path_with_client_name_only() -> None:
    path = build_folder_path("document", client_name="Jane Doe")
    assert path == "/Jane Doe/documents"


def test_build_folder_path_with_address_only() -> None:
    path = build_folder_path("estimate", client_address="42 Elm St")
    assert path == "/42 Elm St/estimates"


def test_build_folder_path_no_client_falls_back_to_unsorted() -> None:
    path = build_folder_path("job_photo")
    assert path.startswith("/Unsorted/")


def test_build_folder_path_unknown_category() -> None:
    path = build_folder_path("unknown_type", client_name="Alice")
    assert path == "/Alice/other"


# ---------------------------------------------------------------------------
# _build_filename tests
# ---------------------------------------------------------------------------


def test_build_filename_with_description() -> None:
    name = _build_filename("damaged railing", "job_photo", index=1)
    assert name == "damaged_railing_001.jpg"


def test_build_filename_without_description() -> None:
    name = _build_filename("", "job_photo", index=2)
    assert name == "photo_002.jpg"


# ---------------------------------------------------------------------------
# upload_to_storage tool tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_upload_writes_file_to_storage(
    test_user: User,
) -> None:
    """upload_to_storage should write to the storage backend with the right path."""
    storage = MockStorageBackend()
    tools = create_file_tools(
        test_user,
        storage,
        pending_media={"https://example.com/media/photo.jpg": b"fake-image-bytes"},
    )
    upload = tools[0].function

    result = await upload(
        file_category="job_photo",
        description="Damaged deck railing",
        client_name="Johnson",
        client_address="116 Virginia Ave",
        original_url="https://example.com/media/photo.jpg",
    )

    assert result.is_error is False
    assert "Uploaded" in result.content
    assert "damaged_deck_railing_001.jpg" in result.content
    assert any("Johnson - 116 Virginia Ave/photos" in key for key in storage.files)


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
        file_category="job_photo",
        description="receipt for fasteners",
        client_name="Loeffler",
        original_url="https://example.com/p.jpg",
    )

    saved = next(iter(storage.metadata.values()))
    assert saved.description == "receipt for fasteners"


@pytest.mark.asyncio()
async def test_upload_to_client_folder(
    test_user: User,
) -> None:
    """Files with client info should go to the client folder."""
    storage = MockStorageBackend()
    tools = create_file_tools(
        test_user,
        storage,
        pending_media={"https://example.com/doc.pdf": b"pdf-bytes"},
    )
    upload = tools[0].function

    await upload(
        file_category="document",
        description="Invoice from supplier",
        client_name="Jane Smith",
        mime_type="application/pdf",
    )

    assert len(storage.files) == 1
    path = next(iter(storage.files))
    assert "Jane Smith/documents/" in path
    assert path.endswith(".pdf")


@pytest.mark.asyncio()
async def test_upload_without_client_goes_to_unsorted(
    test_user: User,
) -> None:
    """Files without client info should go to Unsorted/{date}/."""
    storage = MockStorageBackend()
    tools = create_file_tools(
        test_user,
        storage,
        pending_media={"https://example.com/doc.pdf": b"pdf-bytes"},
    )
    upload = tools[0].function

    await upload(
        file_category="document",
        description="Invoice from supplier",
        mime_type="application/pdf",
    )

    assert len(storage.files) == 1
    path = next(iter(storage.files))
    assert "Unsorted/" in path
    assert path.endswith(".pdf")


@pytest.mark.asyncio()
async def test_upload_no_media_returns_error(
    test_user: User,
) -> None:
    """Upload with no pending media should return error guiding to organize_file."""
    storage = MockStorageBackend()
    tools = create_file_tools(test_user, storage, pending_media={})
    upload = tools[0].function

    result = await upload(file_category="job_photo")
    assert "No file content" in result.content
    assert "organize_file" in result.content
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

    result = await upload(file_category="job_photo", description="Auto selected")
    assert "Uploaded" in result.content
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
        file_category="job_photo",
        original_url="https://example.com/1.jpg",
        client_name="Test Client",
    )
    result2 = await upload(
        file_category="job_photo",
        original_url="https://example.com/2.jpg",
        client_name="Test Client",
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

    await upload(file_category="job_photo", client_name="Fence Client")
    assert len(storage.folders) == 1
    assert "Fence Client" in storage.folders[0]
    assert "/photos" in storage.folders[0]


# ---------------------------------------------------------------------------
# organize_file tool tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_organize_file_moves_to_client_folder(
    test_user: User,
) -> None:
    """organize_file should move an auto-saved file into the client folder."""
    storage = MockStorageBackend()
    await storage.upload_file(b"img-data", "/Unsorted/2026-03-02", "file_001.jpg")

    tools = create_file_tools(test_user, storage)
    organize = tools[1].function

    result = await organize(
        storage_path="/Unsorted/2026-03-02/file_001.jpg",
        file_category="job_photo",
        client_name="John Smith",
        client_address="116 Virginia Ave",
        description="Front porch damage",
    )

    assert "Moved" in result.content
    assert "John Smith - 116 Virginia Ave" in result.content
    assert "front_porch_damage_001.jpg" in result.content
    assert result.is_error is False

    # Verify storage state: old key gone, new key present
    assert "Unsorted/2026-03-02/file_001.jpg" not in storage.files
    assert any("John Smith" in k for k in storage.files)


@pytest.mark.asyncio()
async def test_organize_file_not_found(
    test_user: User,
) -> None:
    """organize_file should return an error if the file is not in storage."""
    storage = MockStorageBackend()
    tools = create_file_tools(test_user, storage)
    organize = tools[1].function

    result = await organize(
        storage_path="/Unsorted/2026-03-02/nonexistent.jpg",
        file_category="job_photo",
        client_name="Jane",
    )
    assert "File not found" in result.content
    assert result.is_error is True


@pytest.mark.asyncio()
async def test_organize_file_already_in_client_folder(
    test_user: User,
) -> None:
    """organize_file should return early if the file is already in a client folder."""
    storage = MockStorageBackend()
    await storage.upload_file(b"img-data", "/Jane/photos", "deck_001.jpg")

    tools = create_file_tools(test_user, storage)
    organize = tools[1].function

    result = await organize(
        storage_path="/Jane/photos/deck_001.jpg",
        file_category="job_photo",
        client_name="Jane",
    )
    assert "already organized" in result.content


@pytest.mark.asyncio()
async def test_organize_file_without_client_returns_error(
    test_user: User,
) -> None:
    """organize_file without client_name or client_address should return an error."""
    storage = MockStorageBackend()
    await storage.upload_file(b"img-data", "/Unsorted/2026-03-02", "file_001.jpg")

    tools = create_file_tools(test_user, storage)
    organize = tools[1].function

    result = await organize(
        storage_path="/Unsorted/2026-03-02/file_001.jpg",
        file_category="job_photo",
    )
    assert "Error" in result.content
    assert "client_name or client_address is required" in result.content
    assert result.is_error is True


@pytest.mark.asyncio()
async def test_organize_file_normalizes_missing_leading_slash(
    test_user: User,
) -> None:
    """organize_file should accept storage_path with or without a leading slash."""
    storage = MockStorageBackend()
    await storage.upload_file(b"img-data", "/Unsorted/2026-03-02", "file_002.jpg")

    tools = create_file_tools(test_user, storage)
    organize = tools[1].function

    result = await organize(
        storage_path="Unsorted/2026-03-02/file_002.jpg",
        file_category="job_photo",
        client_name="Ralph Smith",
    )
    assert result.is_error is False
    assert "Moved" in result.content


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
