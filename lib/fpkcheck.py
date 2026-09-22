#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DBX 飞牛包的打包自检（上架闸门）。

用法：
    fpkcheck.py <package-dir> --expect-version 0.6.19 --expect-platform x86
    fpkcheck.py --fpk dist/dbx_0.6.19_x86.fpk --expect-version 0.6.19 --expect-platform x86

检查项：
  1) 结构与必备文件（manifest / config / cmd / wizard / ICON / app 内容）；
  2) manifest 关键字段与期望版本、架构一致，且没有残留占位符；
  3) ui/config 合法 JSON、声明的入口 ID 与 manifest 的 desktop_applaunchname 对得上；
  4) 图标尺寸（ICON.PNG=64、ICON_256.PNG=256）；
  5) 生命周期脚本语法（bash -n）与可执行位；
  6) 【上架闸门】我们自己写的文件里不能出现本机痕迹（内网 IP / 主机名 / 域名 / 个人标识）；
  7) 出包形态（--fpk）：app.tgz 里必须有 ui/config、bin/dbx-web-bin、dist/index.html。
"""
import argparse
import base64
import json
import os
import re
import struct
import subprocess
import sys
import tarfile
import tempfile

REQUIRED_MANIFEST = [
    'appname', 'version', 'display_name', 'desc', 'source', 'platform',
    'maintainer', 'desktop_uidir', 'desktop_applaunchname',
]
LIFECYCLE = ['main', 'install_init', 'install_callback', 'config_init', 'config_callback',
             'upgrade_init', 'upgrade_callback', 'uninstall_init', 'uninstall_callback']
BINARY_EXT = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.ico', '.icns', '.woff', '.woff2',
              '.ttf', '.otf', '.zip', '.gz', '.tgz', '.bz2', '.xz', '.whl', '.wasm', '.so',
              '.a', '.pdf', '.mp3', '.mp4', '.db', '.sqlite', '.onnx'}
# 我们自己写的文件里允许出现的上游地址（只有项目身份，没有本机/私人信息）
ALLOWED_URL_HOSTS = {'github.com', 'api.github.com', 'raw.githubusercontent.com', 't8y2.github.io'}

problems = []
notes = []


def fail(msg):
    problems.append(msg)


def png_size(path):
    with open(path, 'rb') as fh:
        head = fh.read(33)
    if head[:8] != b'\x89PNG\r\n\x1a\n':
        return None
    return struct.unpack('>II', head[16:24])


def parse_manifest(path):
    out = {}
    for raw in open(path, encoding='utf-8', errors='replace'):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            continue
        k, v = line.split('=', 1)
        out[k.strip()] = v.strip().strip('"')
    return out


def local_identifiers():
    """本机标识：主机名 + 所有网卡 IP（排除回环）+ 环境变量里点名的个人标识。

    只从环境变量传个人标识（DBX_FORBIDDEN=某某账号名），不写进源码 —— 否则闸门自己就成了泄密点。
    """
    ids = set()
    try:
        ids.add(os.uname().nodename)
    except Exception:                                              # noqa: BLE001
        pass
    try:
        out = subprocess.run(['ip', '-4', '-o', 'addr'], capture_output=True, text=True, timeout=10).stdout
        for m in re.finditer(r'inet\s+([0-9.]+)', out):
            ip = m.group(1)
            if ip.startswith('127.'):
                continue        # 回环地址是通用写法（上游前端里到处都有），不算本机痕迹
            ids.add(ip)
    except Exception:                                              # noqa: BLE001
        pass
    for extra in (os.environ.get('DBX_FORBIDDEN') or '').split(','):
        if extra.strip():
            ids.add(extra.strip())
    return {i for i in ids if len(i) > 3}


def scan_text(root, paths, strict_urls):
    """扫描文本文件，命中「本机痕迹」就报错。

    strict_urls=True（我们自己写的元数据/脚本）：还额外查
      * 任何非回环 IP（写死 IP 基本就是本机刚需，不是通用默认值）；
      * 任何非项目白名单的 http(s):// 地址。
    strict_urls=False（上游前端产物这种几万行的第三方文件）：只查**精确**的本机标识
      （本机网卡 IP、主机名、个人标识），避免把回环地址 / 内网示例地址这类
      通用示例值当成泄密（实测上游 bundle 里到处都是，硬拦会变成假闸门）。
    """
    ids = local_identifiers()
    scanned = 0
    for rel in paths:
        full = os.path.join(root, rel)
        if not os.path.isfile(full):
            continue
        ext = os.path.splitext(full)[1].lower()
        if ext in BINARY_EXT:
            continue
        try:
            text = open(full, encoding='utf-8').read()
        except UnicodeDecodeError:
            continue
        scanned += 1
        for ident in ids:
            if ident in text:
                fail('%s 里出现了本机标识「%s」（上架包不能夹带本机信息）' % (rel, ident))
        if not strict_urls:
            continue
        for m in re.finditer(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', text):
            ip = m.group(0)
            if ip.startswith(('127.', '0.0.0.0', '255.')):
                continue
            fail('%s 里出现了非回环 IP：%s' % (rel, ip))
        for m in re.finditer(r'https?://([A-Za-z0-9.\-]+)', text):
            host = m.group(1).lower()
            if host.startswith('127.') or host == 'localhost':
                continue
            if host in ALLOWED_URL_HOSTS:
                continue
            fail('%s 里出现了非项目地址：%s（第三方包只应引用上游/官方地址）' % (rel, m.group(0)))
        if '@VERSION@' in text or '@PLATFORM@' in text or '@CHANGELOG@' in text:
            fail('%s 里还有未替换的占位符' % rel)
    return scanned


def check_dir(root, expect_version, expect_platform):
    # 1) 必备文件
    for rel, kind in [('manifest', 'file'), ('config/privilege', 'file'), ('config/resource', 'file'),
                      ('ICON.PNG', 'file'), ('ICON_256.PNG', 'file'), ('cmd', 'dir'),
                      ('wizard', 'dir'), ('app', 'dir')]:
        p = os.path.join(root, rel)
        if not os.path.exists(p):
            fail('缺少 %s（fnpack 打包检查要求必须存在）' % rel)
        elif kind == 'dir' and not os.path.isdir(p):
            fail('%s 应该是目录' % rel)
        elif kind == 'file' and not os.path.isfile(p):
            fail('%s 应该是文件' % rel)

    # 2) manifest
    mpath = os.path.join(root, 'manifest')
    if os.path.isfile(mpath):
        man = parse_manifest(mpath)
        for key in REQUIRED_MANIFEST:
            if not man.get(key):
                fail('manifest 缺字段：%s' % key)
        if expect_version and man.get('version') != expect_version:
            fail('manifest version=%s，期望 %s' % (man.get('version'), expect_version))
        if expect_platform and man.get('platform') != expect_platform:
            fail('manifest platform=%s，期望 %s' % (man.get('platform'), expect_platform))
        if man.get('platform') not in ('x86', 'arm', 'all'):
            fail('manifest platform 只能是 x86|arm|all（收到 %s）' % man.get('platform'))
        if not man.get('changelog'):
            fail('manifest 缺 changelog（应用中心会展示）')
        uidir = man.get('desktop_uidir') or 'ui'
        launch = man.get('desktop_applaunchname') or ''
        uic = os.path.join(root, 'app', uidir, 'config')
        if not os.path.isfile(uic):
            fail('缺少 app/%s/config（桌面图标注册文件）' % uidir)
        else:
            try:
                ui = json.load(open(uic, encoding='utf-8'))
            except Exception as exc:                               # noqa: BLE001
                fail('app/%s/config 不是合法 JSON：%s' % (uidir, exc))
                ui = {}
            entries = (ui.get('.url') or {})
            if launch and launch not in entries:
                fail('manifest 的 desktop_applaunchname=%s 在 ui/config 里找不到对应入口' % launch)
            for eid, ent in entries.items():
                if not ent.get('title') or not ent.get('icon'):
                    fail('入口 %s 缺 title/icon' % eid)
                if ent.get('type') == 'iframe' and not ent.get('port') and not ent.get('gatewaySocket'):
                    fail('入口 %s 是 iframe 但既没有 port 也没有 gatewaySocket' % eid)

    # 3) 图标尺寸
    for rel, want in [('ICON.PNG', 64), ('ICON_256.PNG', 256)]:
        p = os.path.join(root, rel)
        if os.path.isfile(p):
            size = png_size(p)
            if not size:
                fail('%s 不是合法 PNG' % rel)
            elif size != (want, want):
                fail('%s 尺寸 %s，期望 %dx%d' % (rel, size, want, want))
    for rel in ('app/ui/images/icon_64.png', 'app/ui/images/icon_256.png',
                'app/ui/images/64.png', 'app/ui/images/256.png'):
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            fail('缺少 %s' % rel)

    # 4) 生命周期脚本
    for name in LIFECYCLE:
        p = os.path.join(root, 'cmd', name)
        if not os.path.isfile(p):
            fail('缺少 cmd/%s' % name)
            continue
        if name == 'main' and not os.access(p, os.X_OK):
            fail('cmd/main 没有可执行位')
        rc = subprocess.run(['bash', '-n', p], capture_output=True, text=True)
        if rc.returncode != 0:
            fail('cmd/%s 语法错误：%s' % (name, rc.stderr.strip()[:200]))

    # 5) 向导 JSON
    for name in ('install', 'config', 'uninstall'):
        p = os.path.join(root, 'wizard', name)
        if not os.path.isfile(p):
            fail('缺少 wizard/%s' % name)
            continue
        try:
            json.load(open(p, encoding='utf-8'))
        except Exception as exc:                                   # noqa: BLE001
            fail('wizard/%s 不是合法 JSON：%s' % (name, exc))
    p = os.path.join(root, 'wizard/uninstall')
    if os.path.isfile(p):
        try:
            wiz = json.load(open(p, encoding='utf-8'))
            fields = [it.get('field') for step in wiz for it in step.get('items', [])]
            if 'wizard_delete_data' not in fields:
                fail('wizard/uninstall 必须包含 field=wizard_delete_data（fnpack 校验项）')
        except Exception:                                          # noqa: BLE001
            pass

    # 6) 应用负载
    for rel in ('app/bin/dbx-web-bin', 'app/dist/index.html'):
        if not os.path.isfile(os.path.join(root, rel)):
            fail('缺少 %s' % rel)
    binp = os.path.join(root, 'app/bin/dbx-web-bin')
    if os.path.isfile(binp):
        if not os.access(binp, os.X_OK):
            fail('app/bin/dbx-web-bin 没有可执行位（装上后起不来）')
        if os.path.getsize(binp) < 5 * 1024 * 1024:
            fail('app/bin/dbx-web-bin 只有 %d 字节，像被截断了' % os.path.getsize(binp))

    # 7) 上架闸门
    ours = ['manifest'] + ['cmd/' + n for n in LIFECYCLE] + \
           ['wizard/' + n for n in os.listdir(os.path.join(root, 'wizard'))] + \
           ['config/' + n for n in os.listdir(os.path.join(root, 'config'))] + \
           ['app/ui/config']
    n_ours = scan_text(root, ours, strict_urls=True)
    rest = []
    for base, _dirs, files in os.walk(root):
        for f in files:
            rel = os.path.relpath(os.path.join(base, f), root)
            if rel in ours:
                continue
            rest.append(rel)
    n_rest = scan_text(root, rest, strict_urls=False)
    notes.append('闸门扫描：自有文件 %d 个（严格）+ 其余 %d 个（只查本机标识）' % (n_ours, n_rest))


def check_fpk(path, expect_version, expect_platform):
    if not os.path.isfile(path):
        fail('fpk 不存在：%s' % path)
        return
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(path, 'r:gz') as tf:
            names = tf.getnames()
            tf.extractall(tmp)
        for need in ('manifest', 'app.tgz', 'cmd/main', 'ICON.PNG', 'ICON_256.PNG'):
            if need not in names:
                fail('fpk 里缺少 %s' % need)
        man = parse_manifest(os.path.join(tmp, 'manifest')) if 'manifest' in names else {}
        if expect_version and man.get('version') != expect_version:
            fail('fpk manifest version=%s，期望 %s' % (man.get('version'), expect_version))
        if expect_platform and man.get('platform') != expect_platform:
            fail('fpk manifest platform=%s，期望 %s' % (man.get('platform'), expect_platform))
        uidir = man.get('desktop_uidir') or 'ui'
        if os.path.isfile(os.path.join(tmp, 'app.tgz')):
            inner = os.path.join(tmp, '_app')
            os.makedirs(inner, exist_ok=True)
            with tarfile.open(os.path.join(tmp, 'app.tgz'), 'r:gz') as tf:
                tf.extractall(inner)
                inner_names = tf.getnames()
            for need in ('%s/config' % uidir, 'bin/dbx-web-bin', 'dist/index.html'):
                if need not in inner_names:
                    fail('app.tgz 里缺少 %s' % need)
            if man.get('desktop_applaunchname'):
                uic = os.path.join(inner, uidir, 'config')
                if os.path.isfile(uic):
                    try:
                        entries = (json.load(open(uic, encoding='utf-8')).get('.url') or {})
                        if man['desktop_applaunchname'] not in entries:
                            fail('app.tgz 内 ui/config 里没有入口 %s（桌面图标不会出现）' % man['desktop_applaunchname'])
                    except Exception as exc:                       # noqa: BLE001
                        fail('app.tgz 内 ui/config 非法：%s' % exc)
            notes.append('app.tgz 内容：%d 项' % len(inner_names))


def main():
    ap = argparse.ArgumentParser(description='DBX fpk 打包自检')
    ap.add_argument('dir', nargs='?', help='待打包目录')
    ap.add_argument('--fpk', help='已生成的 .fpk，做出包后核对')
    ap.add_argument('--expect-version')
    ap.add_argument('--expect-platform')
    args = ap.parse_args()

    if args.fpk:
        check_fpk(args.fpk, args.expect_version, args.expect_platform)
    elif args.dir:
        check_dir(args.dir, args.expect_version, args.expect_platform)
    else:
        ap.error('需要给出目录或 --fpk')

    for n in notes:
        print('[fpkcheck] %s' % n)
    if problems:
        for p in problems:
            print('[fpkcheck] ✗ %s' % p, file=sys.stderr)
        print('[fpkcheck] 自检不通过：%d 项' % len(problems), file=sys.stderr)
        return 1
    print('[fpkcheck] ✓ 自检通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
