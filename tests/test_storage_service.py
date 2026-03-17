from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.app.services.storage_service import (
    DropboxStorage,
    GoogleDriveStorage,
    LocalFileStorage,
    get_storage_service,
)
from tests.mocks.storage import MockStorageBackend

# ---------------------------------------------------------------------------
# MockStorageBackend tests (existing)
# ---------------------------------------------------------------------------


@pytest.fixture()
def storage() -> MockStorageBackend:
    return MockStorageBackend()


@pytest.mark.asyncio()
async def test_upload_file(storage: MockStorageBackend) -> None:
    """upload_file should store bytes and return a URL."""
    url = await storage.upload_file(b"pdf-content", "/estimates", "EST-001.pdf")
    assert "EST-001.pdf" in url
    assert storage.files["/estimates/EST-001.pdf"] == b"pdf-content"


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
    names = [f["name"] for f in files]
    assert "photo1.jpg" in names
    assert "photo2.jpg" in names


@pytest.mark.asyncio()
async def test_list_empty_folder(storage: MockStorageBackend) -> None:
    """list_folder on empty folder should return empty list."""
    files = await storage.list_folder("/empty")
    assert files == []


def test_get_storage_service_invalid_provider() -> None:
    """get_storage_service should raise ValueError for unknown provider."""
    mock_settings = MagicMock()
    mock_settings.storage_provider = "invalid"
    with pytest.raises(ValueError, match="Unknown storage provider"):
        get_storage_service(mock_settings)


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


def test_factory_returns_local() -> None:
    """Factory should return LocalFileStorage for 'local' provider."""
    mock_settings = MagicMock()
    mock_settings.storage_provider = "local"
    result = get_storage_service(mock_settings)
    assert isinstance(result, LocalFileStorage)


def test_factory_returns_dropbox() -> None:
    """Factory should return DropboxStorage for 'dropbox' provider."""
    mock_settings = MagicMock()
    mock_settings.storage_provider = "dropbox"
    mock_settings.dropbox_access_token = "fake-token"
    with patch("backend.app.services.storage_service.dropbox.Dropbox"):
        result = get_storage_service(mock_settings)
    assert isinstance(result, DropboxStorage)


# ---------------------------------------------------------------------------
# LocalFileStorage tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def local_storage(tmp_path: object) -> LocalFileStorage:
    return LocalFileStorage(base_dir=str(tmp_path))


@pytest.mark.asyncio()
async def test_local_upload_writes_file(local_storage: LocalFileStorage) -> None:
    """LocalFileStorage should write bytes to disk and return a file:// URL."""
    url = await local_storage.upload_file(b"photo-bytes", "/Job Photos", "site.jpg")
    assert url.startswith("file://")
    assert "site.jpg" in url
    # Verify the file was actually written
    written = local_storage.base_dir / "Job Photos" / "site.jpg"
    assert written.read_bytes() == b"photo-bytes"


@pytest.mark.asyncio()
async def test_local_create_folder(local_storage: LocalFileStorage) -> None:
    """LocalFileStorage should create the directory on disk."""
    result = await local_storage.create_folder("/projects/2026")
    folder = local_storage.base_dir / "projects" / "2026"
    assert folder.is_dir()
    assert result == str(folder)


@pytest.mark.asyncio()
async def test_local_list_folder(local_storage: LocalFileStorage) -> None:
    """LocalFileStorage should list files in the directory."""
    await local_storage.upload_file(b"a", "/docs", "a.txt")
    await local_storage.upload_file(b"b", "/docs", "b.txt")

    files = await local_storage.list_folder("/docs")
    assert len(files) == 2
    names = {f["name"] for f in files}
    assert names == {"a.txt", "b.txt"}


@pytest.mark.asyncio()
async def test_local_list_folder_empty(local_storage: LocalFileStorage) -> None:
    """LocalFileStorage should return [] for a non-existent folder."""
    files = await local_storage.list_folder("/nonexistent")
    assert files == []


# ---------------------------------------------------------------------------
# LocalFileStorage path-traversal tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_local_upload_rejects_path_traversal(local_storage: LocalFileStorage) -> None:
    """upload_file should reject path containing '..' that escapes base_dir."""
    with pytest.raises(ValueError, match="Path escapes storage directory"):
        await local_storage.upload_file(b"evil", "../../etc", "passwd")


@pytest.mark.asyncio()
async def test_local_upload_rejects_filename_traversal(local_storage: LocalFileStorage) -> None:
    """upload_file should reject filename containing '..' that escapes base_dir."""
    with pytest.raises(ValueError, match="Path escapes storage directory"):
        await local_storage.upload_file(b"evil", "/safe", "../../etc/passwd")


@pytest.mark.asyncio()
async def test_local_create_folder_rejects_traversal(local_storage: LocalFileStorage) -> None:
    """create_folder should reject paths that escape base_dir."""
    with pytest.raises(ValueError, match="Path escapes storage directory"):
        await local_storage.create_folder("../../tmp/evil")


@pytest.mark.asyncio()
async def test_local_list_folder_rejects_traversal(local_storage: LocalFileStorage) -> None:
    """list_folder should reject paths that escape base_dir."""
    with pytest.raises(ValueError, match="Path escapes storage directory"):
        await local_storage.list_folder("../../../etc")


@pytest.mark.asyncio()
async def test_local_safe_relative_path_allowed(local_storage: LocalFileStorage) -> None:
    """Paths that resolve inside base_dir (e.g. 'a/../b') should be allowed."""
    url = await local_storage.upload_file(b"ok", "a/../b", "file.txt")
    assert "file.txt" in url
    assert (local_storage.base_dir / "b" / "file.txt").exists()


# ---------------------------------------------------------------------------
# DropboxStorage tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_dbx_client() -> MagicMock:
    client = MagicMock()
    # Default: shared link creation succeeds
    shared_link = MagicMock()
    shared_link.url = "https://dropbox.com/s/abc/file.pdf?dl=0"
    client.sharing_create_shared_link_with_settings.return_value = shared_link
    # Default: list_folder returns entries
    entry = MagicMock()
    entry.name = "photo.jpg"
    entry.path_display = "/photos/photo.jpg"
    folder_result = MagicMock()
    folder_result.entries = [entry]
    client.files_list_folder.return_value = folder_result
    return client


@pytest.fixture()
def dropbox_storage(mock_dbx_client: MagicMock) -> DropboxStorage:
    with patch(
        "backend.app.services.storage_service.dropbox.Dropbox", return_value=mock_dbx_client
    ):
        s = DropboxStorage(access_token="fake-token")
    return s


def test_dropbox_constructor(mock_dbx_client: MagicMock) -> None:
    """DropboxStorage should create a Dropbox client with the token."""
    with patch(
        "backend.app.services.storage_service.dropbox.Dropbox", return_value=mock_dbx_client
    ) as mock_cls:
        DropboxStorage(access_token="my-token")
    mock_cls.assert_called_once_with("my-token")


@pytest.mark.asyncio()
async def test_dropbox_upload_creates_shared_link(
    dropbox_storage: DropboxStorage, mock_dbx_client: MagicMock
) -> None:
    """upload_file should call files_upload and return the shared link URL."""
    url = await dropbox_storage.upload_file(b"data", "/docs", "file.pdf")
    mock_dbx_client.files_upload.assert_called_once()
    mock_dbx_client.sharing_create_shared_link_with_settings.assert_called_once_with(
        "/docs/file.pdf"
    )
    assert url == "https://dropbox.com/s/abc/file.pdf?dl=0"


@pytest.mark.asyncio()
async def test_dropbox_upload_existing_link_fallback(
    dropbox_storage: DropboxStorage, mock_dbx_client: MagicMock
) -> None:
    """When shared link creation fails, should fall back to list_shared_links."""
    import dropbox as dbx_mod
    import dropbox.exceptions

    mock_dbx_client.sharing_create_shared_link_with_settings.side_effect = (
        dbx_mod.exceptions.ApiError("", None, None, None)
    )
    existing_link = MagicMock()
    existing_link.url = "https://dropbox.com/s/existing/file.pdf?dl=0"
    links_result = MagicMock()
    links_result.links = [existing_link]
    mock_dbx_client.sharing_list_shared_links.return_value = links_result

    url = await dropbox_storage.upload_file(b"data", "/docs", "file.pdf")
    assert url == "https://dropbox.com/s/existing/file.pdf?dl=0"


@pytest.mark.asyncio()
async def test_dropbox_upload_no_links_fallback(
    dropbox_storage: DropboxStorage, mock_dbx_client: MagicMock
) -> None:
    """When both create and list fail, should return the file path."""
    import dropbox as dbx_mod
    import dropbox.exceptions

    mock_dbx_client.sharing_create_shared_link_with_settings.side_effect = (
        dbx_mod.exceptions.ApiError("", None, None, None)
    )
    links_result = MagicMock()
    links_result.links = []
    mock_dbx_client.sharing_list_shared_links.return_value = links_result

    url = await dropbox_storage.upload_file(b"data", "/docs", "file.pdf")
    assert url == "/docs/file.pdf"


@pytest.mark.asyncio()
async def test_dropbox_create_folder(
    dropbox_storage: DropboxStorage, mock_dbx_client: MagicMock
) -> None:
    """create_folder should call files_create_folder_v2 and return path."""
    result = await dropbox_storage.create_folder("/Job Photos")
    mock_dbx_client.files_create_folder_v2.assert_called_once_with("/Job Photos")
    assert result == "/Job Photos"


@pytest.mark.asyncio()
async def test_dropbox_create_folder_already_exists(
    dropbox_storage: DropboxStorage, mock_dbx_client: MagicMock
) -> None:
    """create_folder should suppress ApiError if folder already exists."""
    import dropbox as dbx_mod
    import dropbox.exceptions

    mock_dbx_client.files_create_folder_v2.side_effect = dbx_mod.exceptions.ApiError(
        "", None, None, None
    )
    result = await dropbox_storage.create_folder("/existing")
    assert result == "/existing"


@pytest.mark.asyncio()
async def test_dropbox_list_folder(
    dropbox_storage: DropboxStorage, mock_dbx_client: MagicMock
) -> None:
    """list_folder should return entries as [{name, path}] dicts."""
    files = await dropbox_storage.list_folder("/photos")
    mock_dbx_client.files_list_folder.assert_called_once_with("/photos")
    assert files == [{"name": "photo.jpg", "path": "/photos/photo.jpg"}]


# ---------------------------------------------------------------------------
# GoogleDriveStorage tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_drive_service() -> MagicMock:
    service = MagicMock()
    # files().create().execute() returns metadata
    create_result = {"id": "file-123", "webViewLink": "https://drive.google.com/file/d/123/view"}
    service.files.return_value.create.return_value.execute.return_value = create_result
    # files().list().execute() returns file list
    list_result = {
        "files": [
            {"id": "f1", "name": "photo.jpg", "webViewLink": "https://drive.google.com/f1"},
            {"id": "f2", "name": "doc.pdf", "webViewLink": "https://drive.google.com/f2"},
        ]
    }
    service.files.return_value.list.return_value.execute.return_value = list_result
    return service


@pytest.fixture()
def gdrive_storage(mock_drive_service: MagicMock) -> GoogleDriveStorage:
    s = GoogleDriveStorage(credentials_json='{"token": "fake"}')
    s._service = mock_drive_service
    return s


@pytest.mark.asyncio()
async def test_gdrive_upload_returns_web_link(
    gdrive_storage: GoogleDriveStorage, mock_drive_service: MagicMock
) -> None:
    """upload_file should return the webViewLink from Google Drive."""
    url = await gdrive_storage.upload_file(b"data", "folder-id", "doc.pdf")
    assert url == "https://drive.google.com/file/d/123/view"
    mock_drive_service.files.return_value.create.assert_called_once()


@pytest.mark.asyncio()
async def test_gdrive_upload_fallback_to_id(
    gdrive_storage: GoogleDriveStorage, mock_drive_service: MagicMock
) -> None:
    """When webViewLink is missing, should fall back to file id."""
    mock_drive_service.files.return_value.create.return_value.execute.return_value = {
        "id": "file-456"
    }
    url = await gdrive_storage.upload_file(b"data", "folder-id", "doc.pdf")
    assert url == "file-456"


@pytest.mark.asyncio()
async def test_gdrive_create_folder_returns_id(
    gdrive_storage: GoogleDriveStorage, mock_drive_service: MagicMock
) -> None:
    """create_folder should create a folder and return its id."""
    mock_drive_service.files.return_value.create.return_value.execute.return_value = {
        "id": "folder-789"
    }
    result = await gdrive_storage.create_folder("/Job Photos/2026")
    assert result == "folder-789"


@pytest.mark.asyncio()
async def test_gdrive_create_folder_uses_path_segment(
    gdrive_storage: GoogleDriveStorage, mock_drive_service: MagicMock
) -> None:
    """create_folder should use the last path segment as the folder name."""
    mock_drive_service.files.return_value.create.return_value.execute.return_value = {
        "id": "folder-x"
    }
    await gdrive_storage.create_folder("/Job Photos/2026-02-28")
    call_args = mock_drive_service.files.return_value.create.call_args
    body = call_args[1]["body"] if "body" in call_args[1] else call_args[0][0]
    assert body["name"] == "2026-02-28"
    assert body["mimeType"] == "application/vnd.google-apps.folder"


@pytest.mark.asyncio()
async def test_gdrive_list_folder(
    gdrive_storage: GoogleDriveStorage, mock_drive_service: MagicMock
) -> None:
    """list_folder should query with parent and return [{name, path}] dicts."""
    files = await gdrive_storage.list_folder("folder-id")
    assert len(files) == 2
    assert files[0] == {"name": "photo.jpg", "path": "https://drive.google.com/f1"}
    assert files[1] == {"name": "doc.pdf", "path": "https://drive.google.com/f2"}
    mock_drive_service.files.return_value.list.assert_called_once()


# ---------------------------------------------------------------------------
# move_file tests: MockStorageBackend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_mock_move_file(storage: MockStorageBackend) -> None:
    """move_file should move bytes from old key to new key."""
    await storage.upload_file(b"data", "/Unsorted/2026-03-02", "file_001.jpg")
    url = await storage.move_file(
        "/Unsorted/2026-03-02", "file_001.jpg", "/John/photos", "deck_001.jpg"
    )
    assert "/Unsorted/2026-03-02/file_001.jpg" not in storage.files
    assert storage.files["/John/photos/deck_001.jpg"] == b"data"
    assert "deck_001.jpg" in url


@pytest.mark.asyncio()
async def test_mock_move_file_not_found(storage: MockStorageBackend) -> None:
    """move_file should raise FileNotFoundError for missing files."""
    with pytest.raises(FileNotFoundError):
        await storage.move_file("/nope", "missing.jpg", "/dest", "file.jpg")


# ---------------------------------------------------------------------------
# move_file tests: LocalFileStorage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_local_move_file(local_storage: LocalFileStorage) -> None:
    """LocalFileStorage.move_file should rename the file on disk."""
    await local_storage.upload_file(b"img-data", "/Unsorted/2026-03-02", "file_001.jpg")
    url = await local_storage.move_file(
        "/Unsorted/2026-03-02", "file_001.jpg", "/Client/photos", "deck_001.jpg"
    )
    assert url.startswith("file://")
    assert "deck_001.jpg" in url
    # Old file gone, new file exists
    assert not (local_storage.base_dir / "Unsorted" / "2026-03-02" / "file_001.jpg").exists()
    dest = local_storage.base_dir / "Client" / "photos" / "deck_001.jpg"
    assert dest.read_bytes() == b"img-data"


@pytest.mark.asyncio()
async def test_local_move_file_not_found(local_storage: LocalFileStorage) -> None:
    """LocalFileStorage.move_file should raise FileNotFoundError for missing source."""
    with pytest.raises(FileNotFoundError):
        await local_storage.move_file("/nope", "missing.jpg", "/dest", "file.jpg")


@pytest.mark.asyncio()
async def test_local_move_file_rejects_traversal(local_storage: LocalFileStorage) -> None:
    """move_file should reject path-traversal attempts."""
    await local_storage.upload_file(b"data", "/safe", "file.txt")
    with pytest.raises(ValueError, match="Path escapes storage directory"):
        await local_storage.move_file("/safe", "file.txt", "../../etc", "passwd")


# ---------------------------------------------------------------------------
# move_file tests: DropboxStorage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_dropbox_move_file(
    dropbox_storage: DropboxStorage, mock_dbx_client: MagicMock
) -> None:
    """move_file should call files_move_v2 and return a shared link."""
    url = await dropbox_storage.move_file(
        "/Unsorted/2026-03-02", "file_001.jpg", "/Client/photos", "deck_001.jpg"
    )
    mock_dbx_client.files_move_v2.assert_called_once_with(
        "/Unsorted/2026-03-02/file_001.jpg", "/Client/photos/deck_001.jpg"
    )
    assert url == "https://dropbox.com/s/abc/file.pdf?dl=0"


# ---------------------------------------------------------------------------
# move_file tests: GoogleDriveStorage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_gdrive_move_file(
    gdrive_storage: GoogleDriveStorage, mock_drive_service: MagicMock
) -> None:
    """move_file should search, then update parents and name."""
    # Mock search returning one file
    mock_drive_service.files.return_value.list.return_value.execute.return_value = {
        "files": [{"id": "file-abc", "name": "file_001.jpg"}]
    }
    mock_drive_service.files.return_value.update.return_value.execute.return_value = {
        "id": "file-abc",
        "webViewLink": "https://drive.google.com/file/d/abc/view",
    }

    url = await gdrive_storage.move_file(
        "src-folder-id", "file_001.jpg", "dest-folder-id", "deck_001.jpg"
    )
    assert url == "https://drive.google.com/file/d/abc/view"
    mock_drive_service.files.return_value.update.assert_called_once()


@pytest.mark.asyncio()
async def test_gdrive_move_file_not_found(
    gdrive_storage: GoogleDriveStorage, mock_drive_service: MagicMock
) -> None:
    """move_file should raise FileNotFoundError if source file not in Drive."""
    mock_drive_service.files.return_value.list.return_value.execute.return_value = {"files": []}
    with pytest.raises(FileNotFoundError):
        await gdrive_storage.move_file("src", "missing.jpg", "dest", "file.jpg")


# ---------------------------------------------------------------------------
# Per-user isolation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_local_storage_user_subdirectory(tmp_path: Path) -> None:
    """LocalFileStorage with user_id should store files in a subdirectory."""
    storage = LocalFileStorage(base_dir=str(tmp_path), user_id="42")
    await storage.upload_file(b"photo", "/Job Photos", "site.jpg")
    written = tmp_path / "42" / "Job Photos" / "site.jpg"
    assert written.exists()
    assert written.read_bytes() == b"photo"


@pytest.mark.asyncio()
async def test_local_storage_users_are_isolated(tmp_path: Path) -> None:
    """Files from different users should not be visible to each other."""
    s1 = LocalFileStorage(base_dir=str(tmp_path), user_id="1")
    s2 = LocalFileStorage(base_dir=str(tmp_path), user_id="2")

    await s1.upload_file(b"a", "/docs", "a.txt")
    await s2.upload_file(b"b", "/docs", "b.txt")

    files_1 = await s1.list_folder("/docs")
    files_2 = await s2.list_folder("/docs")
    assert [f["name"] for f in files_1] == ["a.txt"]
    assert [f["name"] for f in files_2] == ["b.txt"]


@pytest.mark.asyncio()
async def test_local_storage_no_user_uses_base_dir(tmp_path: Path) -> None:
    """LocalFileStorage without user_id should use the base dir directly."""
    storage = LocalFileStorage(base_dir=str(tmp_path))
    await storage.upload_file(b"data", "/docs", "file.txt")
    written = tmp_path / "docs" / "file.txt"
    assert written.exists()


def test_factory_passes_user_id_to_local(tmp_path: Path) -> None:
    """get_storage_service should pass user.id to LocalFileStorage."""
    mock_settings = MagicMock()
    mock_settings.storage_provider = "local"
    mock_settings.file_storage_base_dir = str(tmp_path)

    user = MagicMock()
    user.id = 99

    result = get_storage_service(mock_settings, user=user)
    assert isinstance(result, LocalFileStorage)
    assert result.base_dir == (tmp_path / "99").resolve()


def test_factory_no_user_local(tmp_path: Path) -> None:
    """get_storage_service without user should not add subdirectory."""
    mock_settings = MagicMock()
    mock_settings.storage_provider = "local"
    mock_settings.file_storage_base_dir = str(tmp_path)

    result = get_storage_service(mock_settings)
    assert isinstance(result, LocalFileStorage)
    assert result.base_dir == tmp_path.resolve()


def test_dropbox_user_prefixes_paths(mock_dbx_client: MagicMock) -> None:
    """DropboxStorage with user_id should prefix paths."""
    with patch(
        "backend.app.services.storage_service.dropbox.Dropbox", return_value=mock_dbx_client
    ):
        s = DropboxStorage(access_token="fake-token", user_id="42")
    assert s._path_prefix == "/42"
    assert s._prefixed("/docs") == "/42/docs"


def test_dropbox_no_user_no_prefix(mock_dbx_client: MagicMock) -> None:
    """DropboxStorage without user_id should not prefix paths."""
    with patch(
        "backend.app.services.storage_service.dropbox.Dropbox", return_value=mock_dbx_client
    ):
        s = DropboxStorage(access_token="fake-token")
    assert s._path_prefix == ""
    assert s._prefixed("/docs") == "/docs"


def test_gdrive_user_path_prefix() -> None:
    """GoogleDriveStorage with user_id should store a path prefix."""
    s = GoogleDriveStorage(credentials_json='{"token": "fake"}', user_id="42")
    assert s._path_prefix == "42/"


def test_gdrive_no_user_no_prefix() -> None:
    """GoogleDriveStorage without user_id should have empty prefix."""
    s = GoogleDriveStorage(credentials_json='{"token": "fake"}')
    assert s._path_prefix == ""
