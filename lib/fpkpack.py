#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯 Python 的 fpk 打包器（不依赖飞牛的 fnpack）。

为什么要它：`fnpack` 是飞牛自带工具，别的 Linux / CI 上没有；而 fpk 本身只是
一个 gzip 过的 tar，格式已经摸清楚（2026-09-22 用 fnpack 1.2.4 的产物逐字节对照）：

    fpk (tar.gz)
    ├── manifest            ← 文本 key = value，末尾会被追加一行 checksum
    ├── cmd/                 ← 生命周期脚本（main / *_init / *_callback）
    ├── config/              ← privilege / resource
    ├── wizard/              ← 安装/配置/卸载向导
    ├── ICON.PNG             ← 64x64
    ├── ICON_256.PNG         ← 256x256
    └── app.tgz (tar.gz)     ← 应用本体：app/ 的内容 + config/
                                （注意：没有 app/ 这层前缀，且 config/ 会被复制进去一份）

  其中 `checksum = <md5(app.tgz)>`（实测就是 app.tgz 的 md5，32 位小写十六进制）。

用法：
    fpkpack.py <组装好的目录> [-o 输出.fpk]
"""
import hashlib
import os
import sys
import tarfile

GNU = tarfile.GNU_FORMAT


def _norm(info):
    """对齐 fnpack：目录条目名不带结尾斜杠。"""
    if info.isdir():
        info.name = info.name.rstrip('/')
    return info


def _add_tree(tf, root, names):
    for name in sorted(names):
        path = os.path.join(root, name)
        if not os.path.exists(path):
            continue
        tf.add(path, arcname=name, recursive=True, filter=_norm)


def build_app_tgz(pkgdir, out_path):
    """app.tgz = app/ 的内容 + config/（fnpack 就是这个行为）。"""
    app_dir = os.path.join(pkgdir, 'app')
    if not os.path.isdir(app_dir):
        raise SystemExit('缺少 app/ 目录：%s' % app_dir)
    names = [n for n in os.listdir(app_dir)]

    with tarfile.open(out_path, 'w:gz', format=GNU) as tf:
        _add_tree(tf, app_dir, names)
        _add_tree(tf, pkgdir, ['config'])          # config/ 也进应用负载


def append_checksum(pkgdir):
    """往 manifest 追加/更新 checksum = md5(app.tgz)。"""
    app_tgz = os.path.join(pkgdir, 'app.tgz')
    digest = hashlib.md5(open(app_tgz, 'rb').read()).hexdigest()
    manifest = os.path.join(pkgdir, 'manifest')
    lines = [l.rstrip('\n') for l in open(manifest, encoding='utf-8')]
    lines = [l for l in lines if not l.startswith('checksum')]
    while lines and not lines[-1].strip():
        lines.pop()
    lines.append('checksum                   = %s' % digest)
    with open(manifest, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')
    return digest


def pack(pkgdir, out_fpk):
    pkgdir = os.path.abspath(pkgdir)
    out_fpk = os.path.abspath(out_fpk)
    if not os.path.isfile(os.path.join(pkgdir, 'manifest')):
        raise SystemExit('找不到 manifest：%s' % pkgdir)

    app_tgz = os.path.join(pkgdir, 'app.tgz')
    if os.path.exists(app_tgz):
        os.remove(app_tgz)
    build_app_tgz(pkgdir, app_tgz)
    digest = append_checksum(pkgdir)

    top = ['ICON.PNG', 'ICON_256.PNG', 'app.tgz', 'cmd', 'config', 'manifest', 'wizard']
    if os.path.exists(out_fpk):
        os.remove(out_fpk)
    with tarfile.open(out_fpk, 'w:gz', format=GNU) as tf:
        for name in top:
            path = os.path.join(pkgdir, name)
            if not os.path.exists(path):
                raise SystemExit('缺少必需项：%s' % name)
            tf.add(path, arcname=name, recursive=True, filter=_norm)

    os.remove(app_tgz)      # 中间产物，下个版本会重建
    return {'fpk': out_fpk, 'size': os.path.getsize(out_fpk), 'checksum': digest}


def main():
    args = list(sys.argv[1:])
    out = None
    if '-o' in args:
        i = args.index('-o')
        out = args[i + 1]
        del args[i:i + 2]
    if len(args) != 1:
        print(__doc__)
        return 2
    pkgdir = args[0]
    if out is None:
        out = os.path.join(os.path.dirname(os.path.abspath(pkgdir)),
                           os.path.basename(os.path.abspath(pkgdir)) + '.fpk')
    info = pack(pkgdir, out)
    print('[fpkpack] 出包: %s  %d 字节  app.tgz md5=%s' % (info['fpk'], info['size'], info['checksum']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
