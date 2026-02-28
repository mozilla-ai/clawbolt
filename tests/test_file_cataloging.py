import pytest
from sqlalchemy.orm import Session

from backend.app.agent.tools.file_tools import (
    _build_filename,
    _build_folder_path,
    _slugify,
    create_file_tools,
)
from backend.app.models import Contractor, MediaFile
from tests.mocks.storage import MockStorageBackend


def test_slugify_basic() -> None:
    assert _slugify("Hello World") == "hello_world"


def test_slugify_special_chars() -> None:
    assert _slugify("A damaged deck railing!") == "a_damaged_deck_railing"


def test_slugify_max_length() -> None:
    result = _slugify("A very long description that exceeds the limit", max_length=15)
    assert len(result) <= 15


def test_build_folder_path_job_photo() -> None:
    path = _build_folder_path("job_photo", job_name="Johnson Deck")
    assert "/Job Photos/" in path
    assert "johnson_deck" in path


def test_build_folder_path_no_job() -> None:
    path = _build_folder_path("document")
    assert "/Documents/" in path


def test_build_folder_path_voice_note() -> None:
    path = _build_folder_path("voice_note")
    assert "/Voice Notes/" in path


def test_build_filename_with_description() -> None:
    name = _build_filename("damaged railing", "job_photo", index=1)
    assert name == "damaged_railing_001.jpg"


def test_build_filename_without_description() -> None:
    name = _build_filename("", "job_photo", index=2)
    assert name == "photo_002.jpg"


def test_build_filename_voice_note() -> None:
    name = _build_filename(None, "voice_note", index=1, extension="mp3")
    assert name == "voice_note_001.mp3"


@pytest.mark.asyncio()
async def test_upload_creates_media_file_record(
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """upload_to_storage should create a MediaFile record."""
    storage = MockStorageBackend()
    tools = create_file_tools(
        db_session,
        test_contractor,
        storage,
        pending_media={"https://twilio.com/media/photo.jpg": b"fake-image-bytes"},
    )
    upload = tools[0].function

    result = await upload(
        file_category="job_photo",
        description="Damaged deck railing",
        job_name="Johnson Deck",
        original_url="https://twilio.com/media/photo.jpg",
    )

    assert "Uploaded" in result
    assert "damaged_deck_railing_001.jpg" in result

    media_file = (
        db_session.query(MediaFile).filter(MediaFile.contractor_id == test_contractor.id).first()
    )
    assert media_file is not None
    assert "damaged_deck_railing_001.jpg" in media_file.storage_path
    assert media_file.storage_url.startswith("https://mock-storage")


@pytest.mark.asyncio()
async def test_upload_to_correct_folder(
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Files should be uploaded to the correct category folder."""
    storage = MockStorageBackend()
    tools = create_file_tools(
        db_session,
        test_contractor,
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
    assert "/Documents/" in path
    assert path.endswith(".pdf")


@pytest.mark.asyncio()
async def test_upload_no_media_returns_error(
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Upload with no pending media should return error message."""
    storage = MockStorageBackend()
    tools = create_file_tools(db_session, test_contractor, storage, pending_media={})
    upload = tools[0].function

    result = await upload(file_category="job_photo")
    assert "No file content" in result


@pytest.mark.asyncio()
async def test_upload_uses_first_media_if_no_url(
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """If no original_url specified, use first available media."""
    storage = MockStorageBackend()
    tools = create_file_tools(
        db_session,
        test_contractor,
        storage,
        pending_media={"https://twilio.com/media/auto.jpg": b"auto-bytes"},
    )
    upload = tools[0].function

    result = await upload(file_category="job_photo", description="Auto selected")
    assert "Uploaded" in result
    assert len(storage.files) == 1


@pytest.mark.asyncio()
async def test_upload_sequential_indexing(
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Multiple uploads to same folder should get sequential indices."""
    storage = MockStorageBackend()
    tools = create_file_tools(
        db_session,
        test_contractor,
        storage,
        pending_media={
            "https://twilio.com/1.jpg": b"img1",
            "https://twilio.com/2.jpg": b"img2",
        },
    )
    upload = tools[0].function

    result1 = await upload(
        file_category="job_photo",
        original_url="https://twilio.com/1.jpg",
        job_name="Test Job",
    )
    result2 = await upload(
        file_category="job_photo",
        original_url="https://twilio.com/2.jpg",
        job_name="Test Job",
    )

    assert "_001." in result1
    assert "_002." in result2


@pytest.mark.asyncio()
async def test_upload_creates_folder(
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Storage folder should be created before upload."""
    storage = MockStorageBackend()
    tools = create_file_tools(
        db_session,
        test_contractor,
        storage,
        pending_media={"https://twilio.com/f.jpg": b"bytes"},
    )
    upload = tools[0].function

    await upload(file_category="job_photo", job_name="Fence Project")
    assert len(storage.folders) == 1
    assert "Job Photos" in storage.folders[0]
