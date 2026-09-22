"""Mask known input regions in WDA PNG evidence without an imaging dependency."""
import math
import struct
import zlib

from .observations import ObservationRejected


def redact_png(raw: bytes, device_size: tuple, regions: list) -> tuple[bytes, tuple[int, int]]:
    """Decode bounded 8-bit RGB/RGBA PNGs; reject other formats, never save raw.

    Masks known input geometry, not arbitrary private content in an image.
    Screen evidence stays local and owner-only even after this redaction.
    """
    def reject():
        raise ObservationRejected("Screenshot format/geometry cannot be safely redacted.")

    if not raw.startswith(b"\x89PNG\r\n\x1a\n") or len(raw) > 40_000_000:
        reject()
    offset, header, compressed, ended = 8, None, bytearray(), False
    while offset + 12 <= len(raw):
        length = struct.unpack_from(">I", raw, offset)[0]
        kind = raw[offset + 4:offset + 8]
        body = raw[offset + 8:offset + 8 + length]
        if offset + length + 12 > len(raw) or zlib.crc32(kind + body) != struct.unpack_from(">I", raw, offset + 8 + length)[0]:
            reject()
        if kind == b"IHDR":
            header = body
        elif kind == b"IDAT":
            compressed.extend(body)
        elif kind == b"IEND":
            ended = length == 0
            break
        offset += length + 12
    if not ended or header is None or len(header) != 13:
        reject()
    width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", header)
    if (not 0 < width * height <= 12_000_000 or depth != 8 or color not in (2, 6)
            or compression or filtering or interlace):
        reject()
    if abs(width / height - device_size[0] / device_size[1]) > 0.02:
        reject()
    channels = 4 if color == 6 else 3
    stride = width * channels
    expected = height * (stride + 1)
    try:
        decoder = zlib.decompressobj()
        data = decoder.decompress(compressed, expected + 1)
    except zlib.error:
        reject()
    if len(data) != expected or not decoder.eof:
        reject()
    previous = bytearray(stride)
    rows = []
    for y in range(height):
        filter_type = data[y * (stride + 1)]
        row = bytearray(data[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if filter_type > 4:
            reject()
        if filter_type:
            for x in range(stride):
                left = row[x - channels] if x >= channels else 0
                up = previous[x]
                corner = previous[x - channels] if x >= channels else 0
                if filter_type == 1:
                    prediction = left
                elif filter_type == 2:
                    prediction = up
                elif filter_type == 3:
                    prediction = (left + up) // 2
                else:
                    p = left + up - corner
                    a, b, c = abs(p - left), abs(p - up), abs(p - corner)
                    prediction = left if a <= b and a <= c else up if b <= c else corner
                row[x] = (row[x] + prediction) & 255
        rows.append(row)
        previous = row
    for x, y, w, h in regions:
        left = max(0, math.floor(x * width / device_size[0]) - 2)
        top = max(0, math.floor(y * height / device_size[1]) - 2)
        right = min(width, math.ceil((x + w) * width / device_size[0]) + 2)
        bottom = min(height, math.ceil((y + h) * height / device_size[1]) + 2)
        if right <= left or bottom <= top:
            continue
        fill = bytes([32, 32, 32] + ([255] if channels == 4 else [])) * (right - left)
        for row in rows[top:bottom]:
            row[left * channels:right * channels] = fill

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    encoded = zlib.compress(b"".join(b"\0" + row for row in rows))
    return raw[:8] + chunk(b"IHDR", header) + chunk(b"IDAT", encoded) + chunk(b"IEND", b""), (width, height)
