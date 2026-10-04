import struct
import unittest
import zlib
from openclaw_iphone.image_evidence import redact_png
from openclaw_iphone.observations import ObservationRejected

def png(width=8, height=16, filter_type=0):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    # Known white RGBA pixels, encoded independently for each PNG filter.
    first_pixel, tail = bytes([255]) * 4, bytes(width * 4 - 4)
    rows = []
    for y in range(height):
        row = {
            0: bytes([255]) * width * 4,
            1: first_pixel + tail,
            2: bytes([255]) * width * 4 if y == 0 else bytes(width * 4),
            3: first_pixel + bytes([128]) * len(tail) if y == 0 else bytes([128]) * 4 + tail,
            4: first_pixel + tail if y == 0 else bytes(width * 4),
        }.get(filter_type, bytes([255]) * width * 4)
        rows.append(bytes([filter_type]) + row)
    data = b"".join(rows)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(data)) + chunk(b"IEND", b"")


class ImageEvidenceTests(unittest.TestCase):






































    def test_redaction_and_invalid_png_never_save_original(self):
        def pixels(data, width, height):
            offset, compressed = 8, bytearray()
            while offset < len(data):
                length = struct.unpack_from(">I", data, offset)[0]
                kind = data[offset + 4:offset + 8]
                body = data[offset + 8:offset + 8 + length]
                if kind == b"IHDR":
                    actual_width, actual_height, depth, color, _, _, _ = struct.unpack(">IIBBBBB", body)
                    self.assertEqual((actual_width, actual_height, depth), (width, height, 8))
                    self.assertIn(color, (2, 6))
                    channels = 4 if color == 6 else 3
                if kind == b"IDAT":
                    compressed.extend(body)
                offset += length + 12
            decoded = zlib.decompress(compressed)
            stride = width * channels + 1
            self.assertEqual(len(decoded), stride * height)
            previous, result = bytes(stride - 1), []
            for y in range(height):
                filtering = decoded[y * stride]
                self.assertIn(filtering, range(5))
                row = bytearray()
                for index, value in enumerate(decoded[y * stride + 1:(y + 1) * stride]):
                    left = row[index - channels] if index >= channels else 0
                    up = previous[index]
                    corner = previous[index - channels] if index >= channels else 0
                    paeth = min((left, up, corner), key=lambda n: abs(left + up - corner - n))
                    prediction = (0, left, up, (left + up) // 2, paeth)[filtering]
                    row.append((value + prediction) % 256)
                result.extend(bytes(row[x:x + channels]) + (b"\xff" if channels == 3 else b"")
                              for x in range(0, len(row), channels))
                previous = row
            return result

        for filtering in range(5):
            data, size = redact_png(png(filter_type=filtering), (8, 16), [(0, 0, 8, 16)])
            self.assertEqual(size, (8, 16))
            self.assertEqual(pixels(data, 8, 16), [bytes([32, 32, 32, 255])] * 128)
        for filtering in range(5):
            data, size = redact_png(png(16, 32, filtering), (8, 16), [(3, 6, 1, 2)])
            self.assertEqual(size, (16, 32))
            self.assertEqual(pixels(data, 16, 32), [
                bytes([32, 32, 32, 255]) if 4 <= x < 10 and 10 <= y < 18 else bytes([255] * 4)
                for y in range(32) for x in range(16)])  # 2x scale plus two-pixel padding.
        for raw in (b"invalid", png()[:-10], png(filter_type=6)):
            with self.assertRaises(ObservationRejected):
                redact_png(raw, (8, 16), [])
