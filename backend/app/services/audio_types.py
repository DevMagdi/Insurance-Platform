"""Recognise the container a call recording arrived in.

A recording is stored as opaque encrypted bytes: providers hand over MP3, WAV
or Ogg depending on who placed the call and how it was configured, and the
column keeps no note of which. That was fine while the only consumer was a
download link, but a browser will not play audio it cannot type, and the
response deliberately carries ``X-Content-Type-Options: nosniff`` -- so
serving everything as ``application/octet-stream`` guarantees silence.

Sniffing here rather than trusting a provider-supplied label is the safer
direction: the bytes are the only thing that survived into the database, and
an unrecognised payload falls back to a download instead of being given a
type it has not earned. The allowlist is audio containers only, so a file
that is secretly markup is still never rendered as markup.
"""

from __future__ import annotations

from typing import Final

DOWNLOAD_MEDIA_TYPE: Final = "application/octet-stream"

# (offset, magic, media type). Ordered longest-prefix first where two formats
# share a lead-in, so a match is unambiguous.
_SIGNATURES: Final[tuple[tuple[int, bytes, str], ...]] = (
    (0, b"ID3", "audio/mpeg"),  # MP3 with a leading tag, what Twilio returns.
    (0, b"RIFF", "audio/wav"),  # Checked against WAVE below.
    (0, b"OggS", "audio/ogg"),
    (0, b"fLaC", "audio/flac"),
    (4, b"ftyp", "audio/mp4"),  # m4a/aac in an MPEG-4 container.
    (0, b"FORM", "audio/aiff"),  # Checked against AIFF below.
)


def detect_audio_media_type(data: bytes) -> str | None:
    """Return an ``audio/*`` type for ``data``, or None when unrecognised.

    None means "offer it as a download": better an extra click than a player
    that silently fails on a format the browser was never told about.
    """
    if len(data) < 12:
        return None
    for offset, magic, media_type in _SIGNATURES:
        if not data.startswith(magic, offset):
            continue
        # RIFF and FORM are generic containers; only the audio ones qualify.
        if magic == b"RIFF" and not data.startswith(b"WAVE", 8):
            continue
        if magic == b"FORM" and not data.startswith(b"AIFF", 8):
            continue
        return media_type
    # A bare MPEG audio frame has no textual magic: 11 set bits, then a
    # version and layer that are both non-reserved.
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        version, layer = (data[1] >> 3) & 0b11, (data[1] >> 1) & 0b11
        if version != 0b01 and layer != 0b00:
            return "audio/mpeg"
    return None
