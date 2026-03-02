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

    async def move_file(
        self, from_path: str, from_filename: str, to_path: str, to_filename: str
    ) -> str:
        src_key = f"{from_path}/{from_filename}"
        if src_key not in self.files:
            msg = f"File not found: {src_key}"
            raise FileNotFoundError(msg)
        dest_key = f"{to_path}/{to_filename}"
        self.files[dest_key] = self.files.pop(src_key)
        return f"https://mock-storage.example.com{dest_key}"

    async def list_folder(self, path: str) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for file_path in self.files:
            if file_path.startswith(path + "/"):
                name = file_path.split("/")[-1]
                result.append({"name": name, "path": file_path})
        return result
