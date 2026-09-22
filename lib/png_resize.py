#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯标准库 PNG 缩放（面积平均 / box filter），用于生成飞牛应用要求的 64/256 图标。

为什么不用 Pillow：打包链要能在任何一台飞牛/普通 Linux 上跑，不引入额外依赖。
用法：
    png_resize.py <in.png> <out.png> <size>
"""
import struct
import sys
import zlib


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def read_png(path):
    data = open(path, 'rb').read()
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise SystemExit('%s: 不是 PNG 文件' % path)
    pos = 8
    width = height = depth = ctype = None
    idat = b''
    while pos < len(data):
        (length,) = struct.unpack('>I', data[pos:pos + 4])
        ctag = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctag == b'IHDR':
            width, height, depth, ctype, comp, filt, interlace = struct.unpack('>IIBBBBB', chunk)
            if depth != 8 or interlace != 0 or ctype not in (2, 6):
                raise SystemExit('%s: 只支持 8bit 非隔行的 RGB/RGBA PNG（depth=%s ctype=%s interlace=%s）'
                                 % (path, depth, ctype, interlace))
        elif ctag == b'IDAT':
            idat += chunk
        elif ctag == b'IEND':
            break
    raw = zlib.decompress(idat)
    channels = 4 if ctype == 6 else 3
    stride = width * channels
    out = bytearray(width * height * channels)
    prev = bytearray(stride)
    p = 0
    for y in range(height):
        ftype = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if ftype == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                upleft = prev[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(left, prev[i], upleft)) & 0xFF
        elif ftype != 0:
            raise SystemExit('%s: 未知的行过滤器 %s' % (path, ftype))
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return width, height, channels, out


def write_png(path, width, height, rgba):
    def chunk(tag, payload):
        return (struct.pack('>I', len(payload)) + tag + payload
                + struct.pack('>I', zlib.crc32(tag + payload) & 0xFFFFFFFF))
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]
    body = (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(bytes(raw), 9))
            + chunk(b'IEND', b''))
    with open(path, 'wb') as fh:
        fh.write(body)


def resize_box(src_w, src_h, channels, pixels, dst_w, dst_h):
    """面积平均缩放（对整数倍缩放即 box filter），保留透明度。"""
    out = bytearray(dst_w * dst_h * 4)
    for dy in range(dst_h):
        y0 = dy * src_h / dst_h
        y1 = (dy + 1) * src_h / dst_h
        iy0, iy1 = int(y0), max(int(y1 + 0.999999), int(y0) + 1)
        for dx in range(dst_w):
            x0 = dx * src_w / dst_w
            x1 = (dx + 1) * src_w / dst_w
            ix0, ix1 = int(x0), max(int(x1 + 0.999999), int(x0) + 1)
            # 透明像素不能参与颜色平均（否则边缘发黑），先按 alpha 加权
            acc = [0.0, 0.0, 0.0, 0.0]  # r,g,b (premultiplied), a
            n = 0
            for yy in range(iy0, min(iy1, src_h)):
                row = yy * src_w * channels
                for xx in range(ix0, min(ix1, src_w)):
                    o = row + xx * channels
                    if channels == 4:
                        a = pixels[o + 3]
                        acc[0] += pixels[o] * a
                        acc[1] += pixels[o + 1] * a
                        acc[2] += pixels[o + 2] * a
                        acc[3] += a
                    else:
                        acc[0] += pixels[o]
                        acc[1] += pixels[o + 1]
                        acc[2] += pixels[o + 2]
                        acc[3] += 255
                    n += 1
            if n == 0:
                n = 1
            a = acc[3] / n
            o = (dy * dst_w + dx) * 4
            if a > 0:
                out[o] = int(round(acc[0] / acc[3]))
                out[o + 1] = int(round(acc[1] / acc[3]))
                out[o + 2] = int(round(acc[2] / acc[3]))
            out[o + 3] = int(round(a))
    return out


def main():
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    src, dst, size = sys.argv[1], sys.argv[2], int(sys.argv[3])
    w, h, channels, pixels = read_png(src)
    rgba = resize_box(w, h, channels, pixels, size, size)
    write_png(dst, size, size, rgba)
    print('png_resize: %s (%dx%d) -> %s (%dx%d)' % (src, w, h, dst, size, size))


if __name__ == '__main__':
    main()
