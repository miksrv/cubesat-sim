"""Mock camera: writes a real file, so everything downstream is exercised.

Producing an actual decodable JPEG rather than returning a fake path means the
mission photo directory, the base64 encoding, the disk-space accounting and the
dashboard's image rendering are all exercised for real. It is a 1x1 pixel, not a
plausible photograph — the point is that it is a valid image file.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

from cubesat.hal.interfaces import Photo

#: The smallest valid JPEG this project needs: 1x1 pixel, with SOI/EOI markers
#: intact so an image viewer accepts it.
_ONE_PIXEL_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPDIzMv/bAEMBCQkJDAsMGAwMGDIcHBwy"
    "MhwcHBwyERwcHBwcERwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBz/wAARCAAB"
    "AAEDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgED"
    "AwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcY"
    "GRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJ"
    "ipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo"
    "6erx8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD+/iiiigD/2Q=="
)


class MockCamera:
    def probe(self) -> bool:
        return True

    def capture(self, path: Path, *, overlay: str | None = None) -> Photo:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_ONE_PIXEL_JPEG)
        return Photo(path=path, width=1, height=1, taken_at=time.time())

    def close(self) -> None:
        return None
