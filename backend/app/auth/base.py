from abc import ABC, abstractmethod
from typing import Any

from backend.app.agent.file_store import ContractorData


class AuthBackend(ABC):
    @abstractmethod
    def get_auth_config(self) -> dict[str, Any]:
        """Return auth config for the frontend."""

    @abstractmethod
    async def authenticate_login(self, credentials: dict[str, str]) -> ContractorData:
        """Validate credentials and return ContractorData."""

    async def on_contractor_created(self, contractor: ContractorData) -> None:  # noqa: B027
        """Hook called after new contractor creation. Override to seed data."""
