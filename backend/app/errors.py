"""Custom exception classes for Backshop."""


class BackshopError(Exception):
    """Base exception for Backshop."""


class MediaProcessingError(BackshopError):
    """Error during media download or processing."""


class AgentError(BackshopError):
    """Error during agent processing."""


class StorageError(BackshopError):
    """Error during cloud storage operations."""


class MessagingError(BackshopError):
    """Error communicating with the messaging service."""
