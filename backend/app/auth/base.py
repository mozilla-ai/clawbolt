from abc import ABC, abstractmethod
from typing import Any

from backend.app.models import User


class AuthBackend(ABC):
    @abstractmethod
    def get_auth_config(self) -> dict[str, Any]:
        """Return auth config for the frontend."""

    @abstractmethod
    async def authenticate_login(self, credentials: dict[str, str]) -> User:
        """Validate credentials and return User."""

    async def on_user_created(self, user: User) -> None:  # noqa: B027
        """Hook called after new user creation. Override to seed data."""
