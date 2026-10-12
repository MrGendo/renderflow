"""真实产物生成库：纯 Python 标准库实现 PNG/GIF/WAV 的编码与 PNG 解码。

不依赖任何第三方包与外部 API（无 API key 需求），全部本地确定性计算：
- png_encode / png_decode：RGB 8-bit PNG（zlib deflate + CRC）
- gif_encode：GIF89a 动画（自带 LZW 压缩器）
- wav_encode：RIFF PCM16 单声道
- 图像绘制辅助：纯色、横条、渐变——用于把真实统计（亮度/时长/直方图）画成图
"""

import math
import struct
import zlib
from typing import Callable, Iterator

# ---------------------------------------------------------------- PNG

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def png_encode(width: int, height: int, pixel_fn: Callable[[int, int], tuple[int, int, int]]) -> bytes:
    """RGB 8-bit PNG。pixel_fn(x,y) -> (r,g,b)。扫描线 filter=0。"""
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter: None
        for x in range(width):
            raw.extend(pixel_fn(x, y))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (_PNG_SIG + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _chunk(b"IEND", b""))


def png_decode(data: bytes) -> tuple[int, int, Callable[[], Iterator[tuple[int, int, int]]]]:
    """解码 8-bit PNG（支持 color type 0/2/3/6），返回 (w, h, 像素迭代器函数)。

    迭代器逐行产出 RGB；调色板图自动经 PLTE 映射。失败抛 ValueError。
    """
    if data[:8] != _PNG_SIG:
        raise ValueError("not a png")
    pos = 8
    width = height = bit_depth = color_type = None
    palette = None
    idat = b""
    while pos < len(data):
        ln, = struct.unpack(">I", data[pos:pos+4])
        tag = data[pos+4:pos+8]
        body = data[pos+8:pos+8+ln]
        if tag == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", body[:10])
        elif tag == b"PLTE":
            palette = [tuple(body[i:i+3]) for i in range(0, len(body), 3)]
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
        pos += 12 + ln
    if bit_depth != 8:
        raise ValueError(f"unsupported bit depth {bit_depth}")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    stride = width * channels

    def pixels():
        raw = zlib.decompress(idat)
        prev = bytearray(stride)
        for y in range(height):
            off = y * (stride + 1)
            ft = raw[off]
            line = bytearray(raw[off+1:off+1+stride])
            if ft == 1:  # Sub
                for i in range(channels, stride):
                    line[i] = (line[i] + line[i-channels]) & 0xFF
            elif ft == 2:  # Up
                for i in range(stride):
                    line[i] = (line[i] + prev[i]) & 0xFF
            elif ft == 3:  # Average
                for i in range(stride):
                    left = line[i-channels] if i >= channels else 0
                    line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
            elif ft == 4:  # Paeth
                for i in range(stride):
                    a = line[i-channels] if i >= channels else 0
                    b = prev[i]
                    c = prev[i-channels] if i >= channels else 0
                    p = a + b - c
                    pa, pb, pc = abs(p-a), abs(p-b), abs(p-c)
                    pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                    line[i] = (line[i] + pr) & 0xFF
            elif ft != 0:
                raise ValueError(f"unsupported filter {ft}")
            prev = line
            for x in range(width):
                o = x * channels
                if color_type == 0:
                    g = line[o]
                    yield g, g, g
                elif color_type == 2:
                    yield line[o], line[o+1], line[o+2]
                elif color_type == 3:
                    yield palette[line[o]] if palette and line[o] < len(palette) else (0, 0, 0)
                elif color_type == 4:
                    g = line[o]
                    a = line[o+1]
                    yield g, g, g
                else:
                    yield line[o], line[o+1], line[o+2]
    return width, height, pixels


# ---------------------------------------------------------------- GIF（GIF89a + LZW）

def _lzw_encode(indices: bytes, min_code_size: int = 8) -> bytes:
    clear, eoi = 1 << min_code_size, (1 << min_code_size) + 1
    table: dict[bytes, int] = {}
    code_size = min_code_size + 1
    next_code = eoi + 1
    bits: list[int] = []

    def emit(code: int) -> None:
        for i in range(code_size):
            bits.append((code >> i) & 1)

    emit(clear)
    w = b""
    for k in indices:
        if not w:
            w = bytes([k])
            continue
        wk = w + bytes([k])
        if wk in table or len(wk) == 1:
            w = wk
        else:
            emit(table.get(w, w[0]))
            table[wk] = next_code
            next_code += 1
            if next_code == (1 << code_size) + 1 and code_size < 12:
                code_size += 1
            if next_code >= 4096:
                emit(clear)
                table.clear()
                next_code = eoi + 1
                code_size = min_code_size + 1
            w = bytes([k])
    if w:
        emit(table.get(w, w[0]))
    emit(eoi)
    out = bytearray()
    for i in range(0, len(bits), 8):
        byte = 0
        for j, b in enumerate(bits[i:i+8]):
            byte |= b << j
        out.append(byte)
    return bytes(out)


def gif_encode(width: int, height: int, frames: list[Callable[[int, int], tuple[int, int, int]]],
               delay_ms: int = 300) -> bytes:
    """多帧 GIF89a。自动收集全局 256 色调色板（不足时补灰阶）。"""
    # 收集颜色
    color_index: dict[tuple[int, int, int], int] = {}
    indexed_frames: list[bytes] = []
    for fn in frames:
        idx = bytearray()
        for y in range(height):
            for x in range(width):
                c = fn(x, y)
                if c not in color_index:
                    if len(color_index) >= 256:  # 超出：量化到已有最近色
                        c = min(color_index, key=lambda p: sum(abs(a-b) for a, b in zip(p, c)))
                    else:
                        color_index[c] = len(color_index)
                idx.append(color_index[c])
        indexed_frames.append(bytes(idx))
    palette = list(color_index.keys())
    while len(palette) < 256:
        palette.append((len(palette) * 7 % 256, len(palette) * 13 % 256, len(palette) * 29 % 256))
    gct = b"".join(bytes(c) for c in palette)

    out = bytearray(b"GIF89a")
    out += struct.pack("<HH", width, height)
    out += bytes([0xF7, 0, 0])  # GCT flag, 256 色, bg=0
    out += gct
    out += b"\x21\xFF\x0BNETSCAPE2.0\x03\x01\x00\x00\x00"  # 循环播放
    for idx in indexed_frames:
        out += b"\x21\xF9\x04" + struct.pack("<HB", 0, 0)  # GCE
        out += bytes([delay_ms // 10]) + b"\x00"  # delay(1/100s), no transparency
        out += struct.pack("<HHHH", 0, 0, width, height)  # 图像描述符（局部无色表）
        out += bytes([0x08])  # LZW min code size
        data = _lzw_encode(idx, 8)
        for i in range(0, len(data), 255):
            chunk = data[i:i+255]
            out += bytes([len(chunk)]) + chunk
        out += b"\x00"  # 块终止
    out += b"\x3B"
    return bytes(out)


# ---------------------------------------------------------------- WAV

def wav_encode(sample_rate: int, samples: list[int]) -> bytes:
    """RIFF PCM16 单声道。samples 为 int16 列表。"""
    data = struct.pack(f"<{len(samples)}h", *samples)
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate,
                                     sample_rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    return header + data


# ---------------------------------------------------------------- 绘制辅助（真实数据 → 图像）

def bar_chart_png(values: list[float], width: int = 480, height: int = 240,
                  title_color=(53, 99, 233)) -> bytes:
    """横向条形图：values ∈ [0,1]。用于镜头亮度/时长等真实统计可视化。"""
    n = max(len(values), 1)
    pad = 10
    bar_h = max((height - 2 * pad) // n, 4)
    vmax = max(values) or 1.0

    def px(x: int, y: int) -> tuple[int, int, int]:
        for i, v in enumerate(values):
            y0 = pad + i * bar_h
            if y0 <= y < y0 + bar_h - 2:
                w = int((width - 2 * pad) * v / vmax)
                if pad <= x < pad + w:
                    return title_color
                return (235, 238, 243)
        return (255, 255, 255)
    return png_encode(width, height, px)


def frame_png(width: int, height: int, seed: int, brightness: float | None,
              label_bar: int) -> bytes:
    """生成一张"镜头帧"：由种子决定的渐变背景 + 亮度条（真实亮度数据）。"""
    rng_vals = [((seed >> (i * 5)) & 0xFF) / 255.0 for i in range(6)]
    b = brightness if brightness is not None else rng_vals[0]

    def px(x: int, y: int) -> tuple[int, int, int]:
        u, v = x / max(width - 1, 1), y / max(height - 1, 1)
        r = int(40 + 120 * u * rng_vals[1] + 60 * b)
        g = int(40 + 120 * v * rng_vals[2] + 60 * b)
        bl = int(60 + 100 * (1 - abs(u - v)) * rng_vals[3] + 40 * b)
        bar_y0 = int(height * 0.78)
        if y >= bar_y0:
            filled = int(width * (label_bar / 100.0))
            return (24, 138, 76) if x < filled else (225, 230, 238)
        return min(r, 255), min(g, 255), min(bl, 255)
    return png_encode(width, height, px)


def tone_samples(seed: int, sample_rate: int, duration_s: float,
                 base_freq: float = 220.0) -> list[int]:
    """由种子决定音高的真实可播放正弦+泛音（确定性）。"""
    rng_seq = [((seed >> (i * 7)) & 0x7F) / 127.0 for i in range(4)]
    f = base_freq * (1 + rng_seq[0])
    n = int(sample_rate * duration_s)
    out = []
    for i in range(n):
        t = i / sample_rate
        env = min(1.0, i / (sample_rate * 0.02), (n - i) / (sample_rate * 0.05))  # 淡入淡出防爆音
        v = (0.6 * math.sin(2 * math.pi * f * t)
             + 0.25 * rng_seq[1] * math.sin(4 * math.pi * f * t)
             + 0.15 * rng_seq[2] * math.sin(6 * math.pi * f * t * (1 + rng_seq[3])))
        out.append(int(max(-1.0, min(1.0, v * env)) * 12000))
    return out
