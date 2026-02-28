from abc import ABC, abstractmethod
from typing import Any

from sqlalchemy.orm import Session

from backend.app.models import Contractor


class AuthBackend(ABC):
    @abstractmethod
    def get_auth_config(self) -> dict[str, Any]:
        """Return auth config for the frontend."""

    @abstractmethod
    def authenticate_login(self, db: Session, credentials: dict[str, str]) -> Contractor:
        """Validate credentials and return Contractor."""

    def on_contractor_created(self, db: Session, contractor: Contractor) -> None:  # noqa: B027
        """Hook called after new contractor creation. Override to seed data."""
