"""Voice replies: turn Gemini's text-to-speech output into audio a phone can play.

Gemini TTS returns raw 16-bit PCM (24 kHz, mono). Browsers play WAV, so for the web demo the PCM is wrapped
in a WAV header (standard library only). WhatsApp wants ogg/opus, so for WhatsApp the WAV is converted with
ffmpeg when it is installed; without ffmpeg the worker simply gets the text reply.
"""
import io
import re
import shutil
import subprocess
import wave

DEFAULT_RATE = 24000


def pcm_to_wav(pcm: bytes, rate: int = DEFAULT_RATE, channels: int = 1, width: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def rate_from_mime(mime: str | None, default: int = DEFAULT_RATE) -> int:
    """Gemini labels its audio like 'audio/L16;codec=pcm;rate=24000'."""
    m = re.search(r"rate=(\d+)", mime or "")
    return int(m.group(1)) if m else default


def speakable(text: str, limit: int = 420) -> str:
    """The part of a reply worth reading aloud: no reference codes, no links, no undo hint, not too long."""
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("Ref:", "http")):
            continue
        lines.append(re.sub(r"https?://\S+", "", line).replace(" / ", ", "))
    out = " ".join(lines).strip()
    if len(out) <= limit:
        return out
    cut = out[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return cut[:end + 1] if end > limit // 2 else cut


def wav_to_ogg(wav: bytes, timeout: int = 20) -> bytes | None:
    """ogg/opus for WhatsApp, or None if ffmpeg is missing or fails."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        done = subprocess.run(
            [ffmpeg, "-loglevel", "error", "-i", "pipe:0", "-c:a", "libopus", "-b:a", "24k", "-f", "ogg", "pipe:1"],
            input=wav, capture_output=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError):
        return None
    return done.stdout if done.returncode == 0 and done.stdout else None
