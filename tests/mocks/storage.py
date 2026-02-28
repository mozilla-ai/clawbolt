from backend.app.services.storage_service import StorageBackend


class MockStorageBackend(StorageBackend):
    """In-memory mock storage for testing."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.folders: list[str] = []

    async def upload_file(self, file_bytes: bytes, path: str, filename: str) -> str:
        full_path = f"{path}/{filename}"
        self.files[full_path] = file_bytes
        return f"https://mock-storage.example.com{full_path}"

    async def create_folder(self, path: str) -> str:
        self.folders.append(path)
        return path

    async def list_folder(self, path: str) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for file_path in self.files:
            if file_path.startswith(path + "/"):
                name = file_path.split("/")[-1]
                result.append({"name": name, "path": file_path})
        return result
