"""QR codes for site posters. A worker scans it with the phone camera; WhatsApp opens with "HAZIRI <code>"
already typed, so checking in is one tap and needs no app, no reading and no typing."""
from urllib.parse import quote

import qrcode
import qrcode.image.svg


def check_in_text(site_code: str) -> str:
    return f"HAZIRI {site_code}"


def check_in_link(site_code: str, wa_number: str) -> str:
    """wa.me link that opens a chat with our WhatsApp number with the check-in text filled in. Without a number
    WhatsApp asks which chat to send it to, which still works for testing."""
    digits = "".join(ch for ch in (wa_number or "") if ch.isdigit())
    return f"https://wa.me/{digits}?text={quote(check_in_text(site_code))}"


def svg(data: str) -> str:
    """Inline SVG markup (no XML declaration), scales to its container."""
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    markup = img.to_string(encoding="unicode")
    return markup[markup.index("<svg"):]
