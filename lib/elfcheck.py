#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯标准库 ELF 检查：确认 dbx-web 是「全静态」二进制。

飞牛的宿主没有 readelf（binutils 未安装），而「静态」是我们要守的硬指标：
  * 不能有 PT_INTERP（"Requesting program interpreter"）；
  * 不能有 DT_NEEDED（动态库依赖）。
用法：elfcheck.py <binary> [--expect-arch x86_64|aarch64]
"""
import struct
import sys

PT_INTERP = 3
SHT_DYNAMIC = 6
DT_NEEDED = 1
MACHINES = {0x3E: 'x86_64', 0xB7: 'aarch64'}


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    path = sys.argv[1]
    expect = None
    if '--expect-arch' in sys.argv:
        expect = sys.argv[sys.argv.index('--expect-arch') + 1]

    data = open(path, 'rb').read()
    if data[:4] != b'\x7fELF':
        raise SystemExit('%s: 不是 ELF 文件' % path)
    is64 = data[4] == 2
    endian = '<' if data[5] == 1 else '>'
    if not is64:
        raise SystemExit('%s: 只处理 64 位 ELF' % path)

    e_type, e_machine = struct.unpack_from(endian + 'HH', data, 0x10)
    e_phoff = struct.unpack_from(endian + 'Q', data, 0x20)[0]
    e_shoff = struct.unpack_from(endian + 'Q', data, 0x28)[0]
    e_phentsize, e_phnum = struct.unpack_from(endian + 'HH', data, 0x36)
    e_shentsize, e_shnum = struct.unpack_from(endian + 'HH', data, 0x3C)

    problems = []
    arch = MACHINES.get(e_machine, 'unknown(0x%x)' % e_machine)

    interp = None
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        if off + 4 > len(data):
            break
        p_type = struct.unpack_from(endian + 'I', data, off)[0]
        if p_type == PT_INTERP:
            p_offset = struct.unpack_from(endian + 'Q', data, off + 8)[0]
            p_filesz = struct.unpack_from(endian + 'Q', data, off + 32)[0]
            interp = data[p_offset:p_offset + p_filesz].split(b'\x00')[0].decode('utf-8', 'replace')
    if interp:
        problems.append('存在 PT_INTERP：%s（不是静态链接）' % interp)

    needed = []
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        if off + 8 > len(data):
            break
        sh_type = struct.unpack_from(endian + 'I', data, off + 4)[0]
        if sh_type != SHT_DYNAMIC:
            continue
        sh_offset = struct.unpack_from(endian + 'Q', data, off + 24)[0]
        sh_size = struct.unpack_from(endian + 'Q', data, off + 32)[0]
        sh_link = struct.unpack_from(endian + 'I', data, off + 40)[0]
        # 解析 .dynstr 取 DT_NEEDED 的名字（尽力而为，失败就只报 tag）
        strtab = b''
        if sh_link < e_shnum:
            soff = e_shoff + sh_link * e_shentsize
            s_off = struct.unpack_from(endian + 'Q', data, soff + 24)[0]
            s_size = struct.unpack_from(endian + 'Q', data, soff + 32)[0]
            strtab = data[s_off:s_off + s_size]
        for j in range(0, sh_size, 16):
            tag, val = struct.unpack_from(endian + 'QQ', data, sh_offset + j)
            if tag != DT_NEEDED:
                continue
            name = ''
            if strtab and val < len(strtab):
                name = strtab[val:].split(b'\x00')[0].decode('utf-8', 'replace')
            needed.append(name or 'dt_needed(0x%x)' % val)
    if needed:
        problems.append('存在动态库依赖（DT_NEEDED）：%s' % ', '.join(needed))

    if expect and arch != expect:
        problems.append('架构不符：期望 %s，实际 %s' % (expect, arch))

    print('elfcheck: %s | arch=%s | type=%d | PT_INTERP=%s | DT_NEEDED=%d'
          % (path, arch, e_type, interp or 'none', len(needed)))

    if problems:
        for p in problems:
            print('elfcheck: 不合格 —— %s' % p, file=sys.stderr)
        raise SystemExit(1)
    print('elfcheck: 通过（完全静态，无解释器、无动态库依赖）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
