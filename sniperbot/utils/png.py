"""Свой минимальный PNG: всё, что нужно графику, — это прямоугольники.

Свеча — прямоугольник, фитиль — узкий прямоугольник, линия входа — пунктир
из коротких прямоугольников. Ради одной этой фигуры тащить на сервер
библиотеку рисования не стоит: PNG без палитры, без фильтров и без
прозрачности занимает тридцать строк и не может сломаться при обновлении.
"""

from __future__ import annotations

import struct
import zlib

Color = tuple[int, int, int]


def _chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


class Canvas:
    """Холст в памяти: строки пикселей по три байта на точку."""

    def __init__(self, width: int, height: int, background: Color) -> None:
        self.width = width
        self.height = height
        self.rows = [bytearray(bytes(background) * width) for _ in range(height)]

    def rect(self, x: float, y: float, width: float, height: float, color: Color) -> None:
        """Закрашенный прямоугольник. Выходящее за край молча обрезается."""
        left, right = max(0, int(x)), min(self.width, int(x + width))
        top, bottom = max(0, int(y)), min(self.height, int(y + height))
        if left >= right or top >= bottom:
            return
        piece = bytes(color) * (right - left)
        for row in self.rows[top:bottom]:
            row[left * 3:right * 3] = piece

    def dashes(self, y: float, color: Color, *, dash: int = 7, gap: int = 6,
               thickness: int = 1) -> None:
        for x in range(0, self.width, dash + gap):
            self.rect(x, y, dash, thickness, color)

    def to_png(self) -> bytes:
        # Фильтр 0 на каждой строке: сжимать построчные разности ради пары
        # килобайт здесь незачем, картинка и так меньше экрана.
        raw = b"".join(b"\x00" + bytes(row) for row in self.rows)
        header = struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)
        return (b"\x89PNG\r\n\x1a\n"
                + _chunk(b"IHDR", header)
                + _chunk(b"IDAT", zlib.compress(raw, 9))
                + _chunk(b"IEND", b""))
