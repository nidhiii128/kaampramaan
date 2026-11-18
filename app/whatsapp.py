"""WhatsApp Cloud API: webhook parsing, signature check, media download and replies."""
import hashlib
import hmac
import logging
from dataclasses import dataclass

import httpx

from .config import Settings

log = logging.getLogger(__name__)
GRAPH = "https://graph.facebook.com"


def verify_signature(body: bytes, header: str | None, app_secret: str) -> bool:
    """Meta signs each webhook POST with HMAC-SHA256 of the raw body (X-Hub-Signature-256)."""
    if not app_secret:
        return True  # signature checks disabled in local development
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


@dataclass
class WAMessage:
    message_id: str
    phone: str
    type: str
    text: str | None = None
    media_id: str | None = None
    mime: str | None = None
    latitude: float | None = None
    longitude: float | None = None


def parse_webhook(payload: dict) -> list[WAMessage]:
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for m in change.get("value", {}).get("messages", []) or []:
                t = m.get("type")
                msg = WAMessage(message_id=m.get("id", ""), phone=m.get("from", ""), type=t)
                if t == "text":
                    msg.text = m.get("text", {}).get("body")
                elif t in ("audio", "image"):
                    media = m.get(t, {})
                    msg.media_id = media.get("id")
                    msg.mime = media.get("mime_type", "")
                    msg.text = media.get("caption")
                elif t == "location":
                    loc = m.get("location", {})
                    msg.latitude, msg.longitude = loc.get("latitude"), loc.get("longitude")
                elif t == "interactive":
                    reply = m.get("interactive", {})
                    msg.text = (reply.get("button_reply") or reply.get("list_reply") or {}).get("title")
                out.append(msg)
    return out


class WhatsAppClient:
    def __init__(self, settings: Settings):
        self.s = settings

    @property
    def configured(self) -> bool:
        return bool(self.s.wa_token and self.s.wa_phone_number_id)

    def _headers(self):
        return {"Authorization": f"Bearer {self.s.wa_token}"}

    def download_media(self, media_id: str) -> tuple[bytes, str]:
        with httpx.Client(timeout=30) as c:
            meta = c.get(f"{GRAPH}/{self.s.wa_api_version}/{media_id}", headers=self._headers())
            meta.raise_for_status()
            info = meta.json()
            data = c.get(info["url"], headers=self._headers())
            data.raise_for_status()
            return data.content, info.get("mime_type", "application/octet-stream")

    def send_text(self, to: str, body: str):
        self._send({"messaging_product": "whatsapp", "to": to, "type": "text",
                    "text": {"body": body[:4096]}})

    def send_document(self, to: str, link: str, filename: str, caption: str | None = None):
        doc = {"link": link, "filename": filename}
        if caption:
            doc["caption"] = caption[:1024]
        self._send({"messaging_product": "whatsapp", "to": to, "type": "document", "document": doc})

    def _send(self, payload: dict):
        if not self.configured:
            log.info("WhatsApp not configured; would send: %s", payload)
            return
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{GRAPH}/{self.s.wa_api_version}/{self.s.wa_phone_number_id}/messages",
                       headers=self._headers(), json=payload)
            if r.status_code >= 400:
                log.error("WhatsApp send failed %s: %s", r.status_code, r.text)
