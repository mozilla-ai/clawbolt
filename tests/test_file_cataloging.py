import pytest

from backend.app.agent.file_store import MediaStore, UserData
from backend.app.agent.file_store import slugify as _slugify
from backend.app.agent.tools.file_tools import (
    _build_client_folder,
    _build_filename,
    auto_save_media,
    build_folder_path,
    create_file_tools,
)
from backend.app.media.download import DownloadedMedia
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


def test_build_folder_path_voice_note_with_client() -> None:
    path = build_folder_path("voice_note", client_name="Bob")
    assert path == "/Bob/voice_notes"


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


def test_build_filename_voice_note() -> None:
    name = _build_filename(None, "voice_note", index=1, extension="mp3")
    assert name == "voice_note_001.mp3"


# ---------------------------------------------------------------------------
# upload_to_storage tool tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_upload_creates_media_file_record(
    test_user: UserData,
) -> None:
    """upload_to_storage should create a MediaData record."""
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

    assert "Uploaded" in result.content
    assert "damaged_deck_railing_001.jpg" in result.content
    assert result.is_error is False


@pytest.mark.asyncio()
async def test_upload_to_client_folder(
    test_user: UserData,
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
    assert "/Jane Smith/documents/" in path
    assert path.endswith(".pdf")


@pytest.mark.asyncio()
async def test_upload_without_client_goes_to_unsorted(
    test_user: UserData,
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
    assert "/Unsorted/" in path
    assert path.endswith(".pdf")


@pytest.mark.asyncio()
async def test_upload_no_media_returns_error(
    test_user: UserData,
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
    test_user: UserData,
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
    test_user: UserData,
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
    test_user: UserData,
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
# auto_save_media tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_auto_save_creates_media_file_records(
    test_user: UserData,
) -> None:
    """auto_save_media should return storage paths for each downloaded file."""
    storage = MockStorageBackend()
    media = [
        DownloadedMedia(
            content=b"image-bytes",
            mime_type="image/jpeg",
            original_url="file_id_1",
            filename="photo.jpg",
        ),
        DownloadedMedia(
            content=b"pdf-bytes",
            mime_type="application/pdf",
            original_url="file_id_2",
            filename="doc.pdf",
        ),
    ]

    saved = await auto_save_media(test_user, storage, media)

    assert len(saved) == 2
    assert len(storage.files) == 2

    # saved is now list[str] (storage paths)
    assert any("/Unsorted/" in p for p in saved)
    assert any(p.endswith(".jpg") for p in saved)
    assert any(p.endswith(".pdf") for p in saved)


@pytest.mark.asyncio()
async def test_auto_save_sequential_filenames(
    test_user: UserData,
) -> None:
    """auto_save_media should produce sequential filenames."""
    storage = MockStorageBackend()
    media = [
        DownloadedMedia(
            content=b"img1", mime_type="image/jpeg", original_url="f1", filename="a.jpg"
        ),
        DownloadedMedia(
            content=b"img2", mime_type="image/jpeg", original_url="f2", filename="b.jpg"
        ),
    ]

    saved = await auto_save_media(test_user, storage, media)

    assert "file_001.jpg" in saved[0]
    assert "file_002.jpg" in saved[1]


@pytest.mark.asyncio()
async def test_auto_save_empty_media_returns_empty(
    test_user: UserData,
) -> None:
    """auto_save_media with no media should return empty list."""
    storage = MockStorageBackend()
    saved = await auto_save_media(test_user, storage, [])
    assert saved == []
    assert len(storage.files) == 0


@pytest.mark.asyncio()
async def test_auto_save_creates_unsorted_folder(
    test_user: UserData,
) -> None:
    """auto_save_media should create the Unsorted/{date} folder."""
    storage = MockStorageBackend()
    media = [
        DownloadedMedia(
            content=b"img", mime_type="image/jpeg", original_url="f1", filename="a.jpg"
        ),
    ]

    await auto_save_media(test_user, storage, media)

    assert len(storage.folders) == 1
    assert storage.folders[0].startswith("/Unsorted/")


# ---------------------------------------------------------------------------
# organize_file tool tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_organize_file_moves_to_client_folder(
    test_user: UserData,
) -> None:
    """organize_file should move an auto-saved file into the client folder."""
    storage = MockStorageBackend()
    # Simulate auto-saved file in Unsorted
    await storage.upload_file(b"img-data", "/Unsorted/2026-03-02", "file_001.jpg")
    media_store = MediaStore(test_user.id)
    await media_store.create(
        original_url="tg_file_id_123",
        mime_type="image/jpeg",
        storage_url="https://mock-storage.example.com/Unsorted/2026-03-02/file_001.jpg",
        storage_path="/Unsorted/2026-03-02/file_001.jpg",
    )

    tools = create_file_tools(test_user, storage)
    organize = tools[1].function

    result = await organize(
        original_url="tg_file_id_123",
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
    assert "/Unsorted/2026-03-02/file_001.jpg" not in storage.files
    assert any("John Smith" in k for k in storage.files)


@pytest.mark.asyncio()
async def test_organize_file_not_found(
    test_user: UserData,
) -> None:
    """organize_file should return an error if the file is not in the store."""
    storage = MockStorageBackend()
    tools = create_file_tools(test_user, storage)
    organize = tools[1].function

    result = await organize(
        original_url="nonexistent_file_id",
        file_category="job_photo",
        client_name="Jane",
    )
    assert "File not found" in result.content
    assert result.is_error is True


@pytest.mark.asyncio()
async def test_organize_file_already_in_client_folder(
    test_user: UserData,
) -> None:
    """organize_file should return early if the file is already in a client folder."""
    storage = MockStorageBackend()
    media_store = MediaStore(test_user.id)
    await media_store.create(
        original_url="tg_file_id_456",
        mime_type="image/jpeg",
        storage_url="https://mock-storage.example.com/Jane/photos/deck_001.jpg",
        storage_path="/Jane/photos/deck_001.jpg",
    )

    tools = create_file_tools(test_user, storage)
    organize = tools[1].function

    result = await organize(
        original_url="tg_file_id_456",
        file_category="job_photo",
        client_name="Jane",
    )
    assert "already organized" in result.content


@pytest.mark.asyncio()
async def test_organize_file_without_client_returns_error(
    test_user: UserData,
) -> None:
    """organize_file without client_name or client_address should return an error."""
    storage = MockStorageBackend()
    media_store = MediaStore(test_user.id)
    await media_store.create(
        original_url="tg_file_id_789",
        mime_type="image/jpeg",
        storage_url="https://mock-storage.example.com/Unsorted/2026-03-02/file_001.jpg",
        storage_path="/Unsorted/2026-03-02/file_001.jpg",
    )

    tools = create_file_tools(test_user, storage)
    organize = tools[1].function

    result = await organize(
        original_url="tg_file_id_789",
        file_category="job_photo",
    )
    assert "Error" in result.content
    assert "client_name or client_address is required" in result.content
    assert result.is_error is True
