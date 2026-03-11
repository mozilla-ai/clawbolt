from abc import ABC, abstractmethod
from typing import Any

from backend.app.agent.file_store import UserData


class AuthBackend(ABC):
    @abstractmethod
    def get_auth_config(self) -> dict[str, Any]:
        """Return auth config for the frontend."""

    @abstractmethod
    async def authenticate_login(self, credentials: dict[str, str]) -> UserData:
        """Validate credentials and return UserData."""

    async def on_user_created(self, user: UserData) -> None:  # noqa: B027
        """Hook called after new user creation. Override to seed data."""
