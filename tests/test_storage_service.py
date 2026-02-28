import pytest

from tests.mocks.storage import MockStorageBackend


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
    from unittest.mock import MagicMock

    from backend.app.services.storage_service import get_storage_service

    mock_settings = MagicMock()
    mock_settings.storage_provider = "invalid"
    with pytest.raises(ValueError, match="Unknown storage provider"):
        get_storage_service(mock_settings)
