import ssl
from unittest.mock import MagicMock, patch

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
async def test_gdrive_get_file_falls_back_to_app_property_when_folder_walk_fails(
    mock_drive_service: MagicMock,
) -> None:
    """Folder rename (or any folder-walk miss) must not turn an existing file into NOT_FOUND.

    Regression for issue #1364: a user moved their folder around in Drive
    (or the per-turn ``_folder_cache`` never learned about it), and the
    legacy ``get_file`` -> ``_resolve_existing_path`` -> ``_find_folder``
    chain returned ``None`` even though the file was still tagged with the
    canonical ``clawbolt_path`` appProperty. ``move_file`` then surfaced
    "File not found at <path>" through the pre-check in ``file_tools``.
    Looking up by appProperty first uses the same handle the upload wrote,
    so the file resolves regardless of folder structure drift.
    """
    s = GoogleDriveStorage(_drive_credentials())
    s._service = mock_drive_service
    s._folder_cache[ROOT_FOLDER_NAME] = "root-folder-id"
    # Cache is empty for the client folder, and the folder-walk query is
    # rigged to find nothing -- the renamed-in-Drive case.
    file_payload = {
        "id": "file-xyz",
        "name": "receipt_001.jpg",
        "parents": ["renamed-parent-id"],
        "appProperties": {"clawbolt_path": "/John Smith - 123 Main/documents/receipt_001.jpg"},
        "webViewLink": "https://drive.google.com/file/d/xyz/view",
    }

    def list_side_effect(*, q: str, **_kwargs: object) -> object:
        if "appProperties has" in q and "receipt_001.jpg" in q:
            return MagicMock(execute=MagicMock(return_value={"files": [file_payload]}))
        # Every folder-walk / name-match query misses.
        return MagicMock(execute=MagicMock(return_value={"files": []}))

    mock_drive_service.files.return_value.list.side_effect = list_side_effect

    saved = await s.get_file("/John Smith - 123 Main/documents/receipt_001.jpg")
    assert saved is not None
    assert saved.path == "/John Smith - 123 Main/documents/receipt_001.jpg"
    assert saved.name == "receipt_001.jpg"
    assert saved.metadata.get("id") == "file-xyz"


@pytest.mark.asyncio()
async def test_gdrive_move_file_uses_actual_parents_for_remove(
    mock_drive_service: MagicMock,
) -> None:
    """removeParents must come from the file's real Drive metadata, not the cached source folder.

    When a folder gets renamed (or the from_path resolves to a stale
    folder id), passing the cached ``from_folder_id`` as ``removeParents``
    is a silent no-op -- the file ends up in both the old AND the new
    folder. Using the file's actual current parents (read from the
    appProperty lookup) keeps the move atomic.
    """
    s = GoogleDriveStorage(_drive_credentials())
    s._service = mock_drive_service
    s._folder_cache[ROOT_FOLDER_NAME] = "root-folder-id"
    s._folder_cache[f"{ROOT_FOLDER_NAME}/dest-folder"] = "dest-folder-id"
    file_payload = {
        "id": "file-abc",
        "name": "file_001.jpg",
        "parents": ["actual-parent-id"],
        "appProperties": {"clawbolt_path": "/src-folder/file_001.jpg"},
    }

    def list_side_effect(*, q: str, **_kwargs: object) -> object:
        if "appProperties has" in q:
            return MagicMock(execute=MagicMock(return_value={"files": [file_payload]}))
        return MagicMock(execute=MagicMock(return_value={"files": []}))

    mock_drive_service.files.return_value.list.side_effect = list_side_effect
    mock_drive_service.files.return_value.update.return_value.execute.return_value = {
        "id": "file-abc",
        "webViewLink": "https://drive.google.com/file/d/abc/view",
    }

    moved = await s.move_file("src-folder", "file_001.jpg", "dest-folder", "file_001.jpg")

    assert moved.metadata.get("id") == "file-abc"
    update_call = mock_drive_service.files.return_value.update.call_args
    assert update_call is not None
    # removeParents pulls from the payload's ``parents``, not the cached
    # source folder id we never had to begin with.
    assert update_call.kwargs.get("removeParents") == "actual-parent-id"
    assert update_call.kwargs.get("addParents") == "dest-folder-id"


@pytest.mark.asyncio()
async def test_gdrive_download_file_falls_back_to_app_property(
    mock_drive_service: MagicMock,
) -> None:
    """download_file must reach files whose folder walk fails but appProperty is intact."""
    s = GoogleDriveStorage(_drive_credentials())
    s._service = mock_drive_service
    s._folder_cache[ROOT_FOLDER_NAME] = "root-folder-id"

    def list_side_effect(*, q: str, **_kwargs: object) -> object:
        if "appProperties has" in q:
            return MagicMock(execute=MagicMock(return_value={"files": [{"id": "file-xyz"}]}))
        return MagicMock(execute=MagicMock(return_value={"files": []}))

    mock_drive_service.files.return_value.list.side_effect = list_side_effect

    content = await s.download_file("/Client/photo.jpg")

    assert content == b"drive-bytes"
    mock_drive_service.files.return_value.get_media.assert_called_once_with(fileId="file-xyz")


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


@pytest.mark.asyncio()
async def test_gdrive_search_files_matches_storage_path_tokens(
    gdrive_storage: GoogleDriveStorage, mock_drive_service: MagicMock
) -> None:
    """``search_files`` must surface files whose ``clawbolt_path`` contains the query.

    Drive's query DSL has no substring operator on ``appProperties.value``, so
    a search for ``"Catch All"`` returns nothing from the native
    ``name contains 'Catch' and name contains 'All'`` query when the files
    are all named ``photo_002.jpg``. ``search_files`` must fall back to a
    broader list and client-side filter on the path appProperty.
    """

    def list_side_effect(*, q: str, **_kwargs: object) -> object:
        # Native name/fullText query: returns nothing (the bug case).
        if "name contains" in q or "fullText contains" in q:
            return MagicMock(execute=MagicMock(return_value={"files": []}))
        # Broad fallback: return three files whose appProperties carry the
        # storage path.
        return MagicMock(
            execute=MagicMock(
                return_value={
                    "files": [
                        {
                            "id": "f1",
                            "name": "photo_002.jpg",
                            "webViewLink": "https://drive.google.com/f1",
                            "appProperties": {"clawbolt_path": "/Catch All/photos/photo_002.jpg"},
                        },
                        {
                            "id": "f2",
                            "name": "photo_001.jpg",
                            "webViewLink": "https://drive.google.com/f2",
                            "appProperties": {"clawbolt_path": "/Catch All/photos/photo_001.jpg"},
                        },
                        {
                            "id": "f3",
                            "name": "unrelated.jpg",
                            "appProperties": {"clawbolt_path": "/Other/folder/unrelated.jpg"},
                        },
                    ]
                }
            )
        )

    mock_drive_service.files.return_value.list.side_effect = list_side_effect

    results = await gdrive_storage.search_files(query="Catch All", limit=10)

    returned_paths = [r.path for r in results]
    assert "/Catch All/photos/photo_002.jpg" in returned_paths
    assert "/Catch All/photos/photo_001.jpg" in returned_paths
    assert "/Other/folder/unrelated.jpg" not in returned_paths


@pytest.mark.asyncio()
async def test_gdrive_search_files_skips_fallback_when_native_query_returns_enough(
    gdrive_storage: GoogleDriveStorage, mock_drive_service: MagicMock
) -> None:
    """When the native query already returns ``limit`` files, no fallback scan.

    Keeps the common case cheap: only pay for the broad list when the
    name/fullText query is empty or short, which is the bug-fix case.
    """
    mock_drive_service.files.return_value.list.return_value.execute.return_value = {
        "files": [
            {
                "id": f"f{i}",
                "name": f"invoice_{i}.pdf",
                "appProperties": {"clawbolt_path": f"/Inbox/invoice_{i}.pdf"},
            }
            for i in range(5)
        ]
    }
    results = await gdrive_storage.search_files(query="invoice", limit=5)
    assert len(results) == 5
    # Single list call total: native query satisfied the bounded request.
    assert mock_drive_service.files.return_value.list.call_count == 1


# ---------------------------------------------------------------------------
# Thread-safety + transient retry (issue: Durham receipt SSL/timeout storm)
# ---------------------------------------------------------------------------


def test_gdrive_get_service_builds_fresh_per_call_in_production() -> None:
    """``_get_service`` must NOT cache the Resource across calls.

    The cached Resource was the root cause of cross-thread TLS corruption
    when the agent fanned uploads out via ``asyncio.to_thread``: one
    Resource wraps one ``httplib2.Http`` which owns one TLS socket, and
    two threads writing to that socket simultaneously surface as
    ``ssl.SSLError: record layer failure`` or ``TimeoutError``.
    """
    s = GoogleDriveStorage(_drive_credentials())
    # No test override: ``_service`` stays None and each call must build.
    built = [MagicMock(name=f"resource-{i}") for i in range(3)]
    with patch.object(s, "_build_service", side_effect=built) as build_mock:
        first = s._get_service()
        second = s._get_service()
        third = s._get_service()
    assert build_mock.call_count == 3
    assert first is built[0]
    assert second is built[1]
    assert third is built[2]


def test_gdrive_get_service_honors_test_override() -> None:
    """Tests can still inject a mock by assigning ``_service`` directly."""
    s = GoogleDriveStorage(_drive_credentials())
    override = MagicMock(name="override")
    s._service = override
    with patch.object(s, "_build_service") as build_mock:
        assert s._get_service() is override
        assert s._get_service() is override
    build_mock.assert_not_called()


@pytest.mark.asyncio()
async def test_gdrive_upload_retries_ssl_error_then_succeeds(
    gdrive_storage: GoogleDriveStorage, mock_drive_service: MagicMock
) -> None:
    """A transient ``SSLError`` should trigger a retry and eventually succeed."""
    gdrive_storage._folder_cache[f"{ROOT_FOLDER_NAME}/folder-id"] = "folder-id"
    mock_drive_service.files.return_value.create.return_value.execute.side_effect = [
        ssl.SSLError("record layer failure"),
        {"id": "file-after-retry", "webViewLink": "https://drive.google.com/file/d/after/view"},
    ]
    # Patch sleep so the test does not wait on the backoff.
    with patch("backend.app.services.storage_service.asyncio.sleep", new=_fake_sleep):
        saved = await gdrive_storage.upload_file(b"data", "folder-id", "doc.pdf")
    assert saved.metadata.get("id") == "file-after-retry"
    assert mock_drive_service.files.return_value.create.return_value.execute.call_count == 2


@pytest.mark.asyncio()
async def test_gdrive_upload_retries_timeout_then_succeeds(
    gdrive_storage: GoogleDriveStorage, mock_drive_service: MagicMock
) -> None:
    """A transient ``TimeoutError`` should trigger a retry and eventually succeed."""
    gdrive_storage._folder_cache[f"{ROOT_FOLDER_NAME}/folder-id"] = "folder-id"
    mock_drive_service.files.return_value.create.return_value.execute.side_effect = [
        TimeoutError("read operation timed out"),
        {"id": "file-after-retry"},
    ]
    with patch("backend.app.services.storage_service.asyncio.sleep", new=_fake_sleep):
        saved = await gdrive_storage.upload_file(b"data", "folder-id", "doc.pdf")
    assert saved.metadata.get("id") == "file-after-retry"


@pytest.mark.asyncio()
async def test_gdrive_upload_gives_up_after_max_retries(
    gdrive_storage: GoogleDriveStorage, mock_drive_service: MagicMock
) -> None:
    """Repeated transient errors past the retry budget should surface as RuntimeError."""
    gdrive_storage._folder_cache[f"{ROOT_FOLDER_NAME}/folder-id"] = "folder-id"
    mock_drive_service.files.return_value.create.return_value.execute.side_effect = ssl.SSLError(
        "record layer failure"
    )
    with (
        patch("backend.app.services.storage_service.asyncio.sleep", new=_fake_sleep),
        pytest.raises(RuntimeError, match="Google Drive upload failed"),
    ):
        await gdrive_storage.upload_file(b"data", "folder-id", "doc.pdf")
    # 3 attempts, all failed.
    assert mock_drive_service.files.return_value.create.return_value.execute.call_count == 3


@pytest.mark.asyncio()
async def test_gdrive_upload_does_not_retry_http_error(
    gdrive_storage: GoogleDriveStorage, mock_drive_service: MagicMock
) -> None:
    """``HttpError`` is an HTTP-level failure, not a transient network blip.

    Retrying a 4xx/5xx ``HttpError`` would mask real backend problems and
    delay the user-facing error. Only the transient network classes are
    retried.
    """
    pytest.importorskip("googleapiclient")
    from googleapiclient.errors import HttpError

    gdrive_storage._folder_cache[f"{ROOT_FOLDER_NAME}/folder-id"] = "folder-id"
    resp = MagicMock(status=500, reason="Internal Server Error")
    mock_drive_service.files.return_value.create.return_value.execute.side_effect = HttpError(
        resp, b"error"
    )
    with pytest.raises(RuntimeError, match="Google Drive upload failed"):
        await gdrive_storage.upload_file(b"data", "folder-id", "doc.pdf")
    # No retry on HttpError: single attempt only.
    assert mock_drive_service.files.return_value.create.return_value.execute.call_count == 1


async def _fake_sleep(_delay: float) -> None:
    """Drop-in replacement for ``asyncio.sleep`` in tests that exercise retry backoff."""
    return None
