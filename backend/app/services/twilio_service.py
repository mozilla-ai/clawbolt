import asyncio
from collections.abc import Generator

from twilio.rest import Client as TwilioClient

from backend.app.config import Settings, settings


class TwilioService:
    def __init__(self, svc_settings: Settings | None = None) -> None:
        s = svc_settings or settings
        self.client = TwilioClient(s.twilio_account_sid, s.twilio_auth_token)
        self.from_number = s.twilio_phone_number

    async def send_sms(self, to: str, body: str) -> str:
        """Send a text-only SMS. Returns message SID."""
        message = await asyncio.to_thread(
            self.client.messages.create,
            to=to,
            from_=self.from_number,
            body=body,
        )
        return message.sid

    async def send_mms(self, to: str, body: str, media_url: str) -> str:
        """Send an MMS with one media attachment. Returns message SID."""
        message = await asyncio.to_thread(
            self.client.messages.create,
            to=to,
            from_=self.from_number,
            body=body,
            media_url=[media_url],
        )
        return message.sid

    async def send_message(self, to: str, body: str, media_urls: list[str] | None = None) -> str:
        """Send SMS or MMS based on whether media_urls is provided."""
        if media_urls:
            message = await asyncio.to_thread(
                self.client.messages.create,
                to=to,
                from_=self.from_number,
                body=body,
                media_url=media_urls,
            )
        else:
            message = await asyncio.to_thread(
                self.client.messages.create,
                to=to,
                from_=self.from_number,
                body=body,
            )
        return message.sid


def get_twilio_service() -> Generator[TwilioService]:
    """FastAPI dependency for TwilioService (overridable in tests)."""
    yield TwilioService()
