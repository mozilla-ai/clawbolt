from unittest.mock import MagicMock

import pytest

from backend.app.services.storage_service import (
    ROOT_FOLDER_NAME,
    DriveOAuthCredentials,
    GoogleDriveStorage,
)
from tests.mocks.storage import MockStorageBackend

# ---------------------------------------------------------------------------
# MockStorageBackend tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage() -> MockStorageBackend:
    return MockStorageBackend()


@pytest.mark.asyncio()
async def test_upload_file(storage: MockStorageBackend) -> None:
    """upload_file should store bytes and return SavedFile metadata."""
    saved = await storage.upload_file(b"pdf-content", "/estimates", "EST-001.pdf")
    assert saved.path == "/estimates/EST-001.pdf"
    assert saved.name == "EST-001.pdf"
    assert "EST-001.pdf" in saved.web_view_link
    assert storage.files["estimates/EST-001.pdf"] == b"pdf-content"


@pytest.mark.asyncio()
async def test_create_folder(storage: MockStorageBackend) -> None:
    """create_folder should register the folder path."""
    path = await storage.create_folder("/Job Photos/2026-02-28")
    assert path == "/Job Photos/2026-02-28"
    assert "/Job Photos/2026-02-28" in storage.folders


@pytest.mark.asyncio()
async def test_list_folder(storage: MockStorageBackend) -> None:
    """list_folder should return files in the specified path."""
    await storage.upload_file(b"photo1", "/photos", "photo1.jpg")
    await storage.upload_file(b"photo2", "/photos", "photo2.jpg")
    await storage.upload_file(b"other", "/docs", "readme.txt")

    files = await storage.list_folder("/photos")
    assert len(files) == 2
    names = [f.name for f in files]
    assert "photo1.jpg" in names
    assert "photo2.jpg" in names


@pytest.mark.asyncio()
async def test_list_empty_folder(storage: MockStorageBackend) -> None:
    """list_folder on empty folder should return empty list."""
    files = await storage.list_folder("/empty")
    assert files == []


@pytest.mark.asyncio()
async def test_mock_download_file(storage: MockStorageBackend) -> None:
    """download_file should return stored bytes by logical path."""
    await storage.upload_file(b"photo1", "/photos", "photo1.jpg")

    content = await storage.download_file("/photos/photo1.jpg")

    assert content == b"photo1"


@pytest.mark.asyncio()
async def test_mock_move_file(storage: MockStorageBackend) -> None:
    """move_file should move bytes from old key to new key."""
    await storage.upload_file(b"data", "/Unsorted/2026-03-02", "file_001.jpg")
    moved = await storage.move_file(
        "/Unsorted/2026-03-02", "file_001.jpg", "/John/photos", "deck_001.jpg"
    )
    assert "Unsorted/2026-03-02/file_001.jpg" not in storage.files
    assert storage.files["John/photos/deck_001.jpg"] == b"data"
    assert moved.name == "deck_001.jpg"
    assert moved.path == "/John/photos/deck_001.jpg"


@pytest.mark.asyncio()
async def test_mock_move_file_not_found(storage: MockStorageBackend) -> None:
    """move_file should raise FileNotFoundError for missing files."""
    with pytest.raises(FileNotFoundError):
        await storage.move_file("/nope", "missing.jpg", "/dest", "file.jpg")


# ---------------------------------------------------------------------------
# GoogleDriveStorage tests
# ---------------------------------------------------------------------------


def _drive_credentials() -> DriveOAuthCredentials:
    return DriveOAuthCredentials(
        access_token="fake-access-token",
        refresh_token="fake-refresh-token",
        client_id="fake-client-id",
        client_secret="fake-client-secret",
    )


@pytest.fixture()
def mock_drive_service() -> MagicMock:
    service = MagicMock()
    create_result = {"id": "file-123", "webViewLink": "https://drive.google.com/file/d/123/view"}
    service.files.return_value.create.return_value.execute.return_value = create_result
    list_result = {
        "files": [
            {"id": "f1", "name": "photo.jpg", "webViewLink": "https://drive.google.com/f1"},
            {"id": "f2", "name": "doc.pdf", "webViewLink": "https://drive.google.com/f2"},
        ]
    }
    service.files.return_value.list.return_value.execute.return_value = list_result
    service.files.return_value.get_media.return_value.execute.return_value = b"drive-bytes"
    return service


@pytest.fixture()
def gdrive_storage(mock_drive_service: MagicMock) -> GoogleDriveStorage:
    s = GoogleDriveStorage(_drive_credentials())
    s._service = mock_drive_service
    # Pre-seed the root-folder cache so individual tests can exercise their
    # own subpath lookups without each test having to wire up the root
    # ``Clawbolt`` folder lookup.
    s._folder_cache[ROOT_FOLDER_NAME] = "root-folder-id"
    return s


@pytest.mark.asyncio()
async def test_gdrive_upload_returns_saved_file(
    gdrive_storage: GoogleDriveStorage, mock_drive_service: MagicMock
) -> None:
    """upload_file should return SavedFile carrying the Drive metadata."""
    gdrive_storage._folder_cache[f"{ROOT_FOLDER_NAME}/folder-id"] = "folder-id"
    saved = await gdrive_storage.upload_file(b"data", "folder-id", "doc.pdf")
    assert saved.web_view_link == "https://drive.google.com/file/d/123/view"
    assert saved.metadata.get("id") == "file-123"
    mock_drive_service.files.return_value.create.assert_called_once()


@pytest.mark.asyncio()
async def test_gdrive_upload_records_storage_path_on_metadata(
    gdrive_storage: GoogleDriveStorage, mock_drive_service: MagicMock
) -> None:
    """upload_file should round-trip the storage path through appProperties."""
    gdrive_storage._folder_cache[f"{ROOT_FOLDER_NAME}/folder-id"] = "folder-id"
    mock_drive_service.files.return_value.create.return_value.execute.return_value = {
        "id": "file-789",
        "appProperties": {"clawbolt_path": "/folder-id/doc.pdf"},
    }
    saved = await gdrive_storage.upload_file(b"data", "folder-id", "doc.pdf")
    assert saved.path == "/folder-id/doc.pdf"


@pytest.mark.asyncio()
async def test_gdrive_create_folder_returns_id(
    gdrive_storage: GoogleDriveStorage, mock_drive_service: MagicMock
) -> None:
    """create_folder should resolve the path and return its Drive folder ID."""
    mock_drive_service.files.return_value.list.return_value.execute.return_value = {"files": []}
    mock_drive_service.files.return_value.create.return_value.execute.side_effect = [
        {"id": "job-photos-id"},
        {"id": "date-folder-id"},
    ]
    result = await gdrive_storage.create_folder("/Job Photos/2026")
    assert result == "date-folder-id"


@pytest.mark.asyncio()
async def test_gdrive_create_folder_reuses_existing(
    gdrive_storage: GoogleDriveStorage, mock_drive_service: MagicMock
) -> None:
    """create_folder should reuse existing folders instead of creating duplicates."""
    mock_drive_service.files.return_value.list.return_value.execute.return_value = {
        "files": [{"id": "existing-id"}]
    }
    result = await gdrive_storage.create_folder("/Unsorted")
    assert result == "existing-id"
    mock_drive_service.files.return_value.create.assert_not_called()


@pytest.mark.asyncio()
async def test_gdrive_list_folder(
    gdrive_storage: GoogleDriveStorage, mock_drive_service: MagicMock
) -> None:
    """list_folder should query with resolved folder ID and return SavedFile entries."""
    gdrive_storage._folder_cache[f"{ROOT_FOLDER_NAME}/folder-id"] = "folder-id"
    files = await gdrive_storage.list_folder("folder-id")
    assert len(files) == 2
    assert files[0].name == "photo.jpg"
    assert files[0].path.endswith("/photo.jpg")
    assert files[0].web_view_link == "https://drive.google.com/f1"
    assert files[1].name == "doc.pdf"
    mock_drive_service.files.return_value.list.assert_called_once()


@pytest.mark.asyncio()
async def test_gdrive_download_file(
    gdrive_storage: GoogleDriveStorage, mock_drive_service: MagicMock
) -> None:
    """download_file should locate a file by logical path and return its bytes."""
    gdrive_storage._folder_cache[f"{ROOT_FOLDER_NAME}/Client"] = "folder-id"
    mock_drive_service.files.return_value.list.return_value.execute.return_value = {
        "files": [{"id": "file-123"}]
    }

    content = await gdrive_storage.download_file("/Client/photo.jpg")

    assert content == b"drive-bytes"
    mock_drive_service.files.return_value.get_media.assert_called_once_with(fileId="file-123")


@pytest.mark.asyncio()
async def test_gdrive_move_file(
    gdrive_storage: GoogleDriveStorage, mock_drive_service: MagicMock
) -> None:
    """move_file should resolve paths, search, then update parents and name."""
    gdrive_storage._folder_cache[f"{ROOT_FOLDER_NAME}/src-folder"] = "src-folder-id"
    gdrive_storage._folder_cache[f"{ROOT_FOLDER_NAME}/dest-folder"] = "dest-folder-id"
    mock_drive_service.files.return_value.list.return_value.execute.return_value = {
        "files": [{"id": "file-abc", "name": "file_001.jpg"}]
    }
    mock_drive_service.files.return_value.update.return_value.execute.return_value = {
        "id": "file-abc",
        "webViewLink": "https://drive.google.com/file/d/abc/view",
    }

    moved = await gdrive_storage.move_file(
        "src-folder", "file_001.jpg", "dest-folder", "deck_001.jpg"
    )
    assert moved.web_view_link == "https://drive.google.com/file/d/abc/view"
    assert moved.path == "/dest-folder/deck_001.jpg"
    mock_drive_service.files.return_value.update.assert_called_once()


@pytest.mark.asyncio()
async def test_gdrive_move_file_not_found(
    gdrive_storage: GoogleDriveStorage, mock_drive_service: MagicMock
) -> None:
    """move_file should raise FileNotFoundError if source file not in Drive."""
    gdrive_storage._folder_cache[f"{ROOT_FOLDER_NAME}/src"] = "src-folder-id"
    gdrive_storage._folder_cache[f"{ROOT_FOLDER_NAME}/dest"] = "dest-folder-id"
    mock_drive_service.files.return_value.list.return_value.execute.return_value = {"files": []}
    with pytest.raises(FileNotFoundError):
        await gdrive_storage.move_file("src", "missing.jpg", "dest", "file.jpg")


@pytest.mark.asyncio()
async def test_gdrive_resolve_path_caches_results(
    gdrive_storage: GoogleDriveStorage, mock_drive_service: MagicMock
) -> None:
    """_resolve_path should cache folder IDs to avoid repeated API calls."""
    mock_drive_service.files.return_value.list.return_value.execute.return_value = {"files": []}
    mock_drive_service.files.return_value.create.return_value.execute.return_value = {
        "id": "new-folder-id"
    }
    await gdrive_storage._resolve_path("/Unsorted")
    folder_id = await gdrive_storage._resolve_path("/Unsorted")
    assert folder_id == "new-folder-id"
    assert mock_drive_service.files.return_value.list.return_value.execute.call_count == 1
    assert mock_drive_service.files.return_value.create.return_value.execute.call_count == 1


@pytest.mark.asyncio()
async def test_gdrive_upload_wraps_api_error(
    gdrive_storage: GoogleDriveStorage, mock_drive_service: MagicMock
) -> None:
    """upload_file should wrap Google API errors in RuntimeError."""
    pytest.importorskip("googleapiclient")
    from googleapiclient.errors import HttpError

    gdrive_storage._folder_cache[f"{ROOT_FOLDER_NAME}/folder-id"] = "folder-id"
    resp = MagicMock(status=500, reason="Internal Server Error")
    mock_drive_service.files.return_value.create.return_value.execute.side_effect = HttpError(
        resp, b"error"
    )
    with pytest.raises(RuntimeError, match="Google Drive upload failed"):
        await gdrive_storage.upload_file(b"data", "folder-id", "doc.pdf")


@pytest.mark.asyncio()
async def test_gdrive_root_folder_created_on_first_resolve(
    mock_drive_service: MagicMock,
) -> None:
    """The first path resolution should create (or find) the Clawbolt root folder."""
    s = GoogleDriveStorage(_drive_credentials())
    s._service = mock_drive_service
    mock_drive_service.files.return_value.list.return_value.execute.return_value = {"files": []}
    mock_drive_service.files.return_value.create.return_value.execute.side_effect = [
        {"id": "clawbolt-root-id"},
        {"id": "unsorted-id"},
    ]
    folder_id = await s.create_folder("/Unsorted")
    assert folder_id == "unsorted-id"
    assert s._folder_cache[ROOT_FOLDER_NAME] == "clawbolt-root-id"
    assert s._folder_cache[f"{ROOT_FOLDER_NAME}/Unsorted"] == "unsorted-id"
