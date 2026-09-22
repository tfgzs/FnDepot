#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 / 更新 FnDepot 的源清单 fnpack.json（schema_version 2）。

格式来自真实源（shuangji66/FnDepot 等）实测：

    {
      "schema_version": 2,
      "source_info": {"name", "author", "homepage", "description"},
      "apps": {
        "<AppKey>": {
          "display_name", "desc", "platform": ["x86"|"arm"|"all"],
          "categories": ["系统工具"], "icon_url": "<AppKey>/ICON.PNG",
          "preview_urls": [...], "readme_url": "<AppKey>/README.md",
          "bug_report_url", "maintainer", "maintainer_url",
          "distributor", "distributor_url",
          "run_as": "package", "install_type": "", "is_docker": false,
          "service_port": "4224",
          "releases": {"0.6.19": {"changelog": "...",
                                  "packages": {"x86": {"download_url", "sha256", "size"}}}}
        }
      }
    }

幂等：同一个版本重复跑只更新 sha256/size/url，不会重复插入；不同架构合并进同一个 release。

用法：
    gen-fnpack.py --fpk dist/dbx_0.6.19_x86.fpk --provenance dist/PROVENANCE-0.6.19-x64.txt \
                  --download-url https://github.com/<owner>/<repo>/releases/download/v0.6.19/dbx_0.6.19_x86.fpk \
                  --out fnpack.json [--changelog "..." ] [--repo-url ...]
"""
import argparse
import hashlib
import json
import os
import re
import sys

APP_KEY = 'dbx'
SOURCE_INFO = {
    'name': 'DBX 飞牛源',
    'author': 'tfgzs',
    'homepage': 'https://github.com/tfgzs/FnDepot',
    'description': 'DBX 数据库管理工具的飞牛 fnOS 原生版源（跟随上游发布，静态二进制、免 Docker）。',
}
APP_META = {
    'display_name': 'DBX 数据库管理',
    'desc': '一站式数据库管理工具：MySQL / PostgreSQL / SQLite / Redis / MongoDB / ClickHouse / '
            'SQL Server / Oracle / DuckDB / Elasticsearch 等 90+ 种数据库，内建 SQL 编辑器、数据网格、'
            '表结构对比与 AI 助手。飞牛原生版，无需 Docker。',
    'platform': ['x86'],
    'categories': ['开发工具'],
    'icon_url': '%s/ICON.PNG' % APP_KEY,
    'preview_urls': [],
    'readme_url': '%s/README.md' % APP_KEY,
    'bug_report_url': '',
    'maintainer': 'DBX',
    'maintainer_url': 'https://github.com/t8y2/dbx',
    'distributor': 'tfgzs',
    'distributor_url': 'https://github.com/tfgzs/FnDepot',
    'run_as': 'package',
    'install_type': '',
    'is_docker': False,
    'service_port': '4224',
}


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def parse_provenance(path):
    out = {}
    if path and os.path.exists(path):
        for line in open(path, encoding='utf-8', errors='replace'):
            if '=' in line:
                k, v = line.split('=', 1)
                out[k.strip()] = v.strip()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--fpk', required=True)
    ap.add_argument('--provenance')
    ap.add_argument('--download-url', required=True)
    ap.add_argument('--out', default='fnpack.json')
    ap.add_argument('--changelog')
    ap.add_argument('--repo-url', default=SOURCE_INFO['homepage'])
    ap.add_argument('--arch', default=None, help='x86|arm（默认从文件名推断）')
    args = ap.parse_args()

    m = re.search(r'_(\d+\.\d+\.\d+)_(x86|arm)\.fpk$', os.path.basename(args.fpk))
    if not m and not args.arch:
        sys.exit('无法从 %s 推断版本/架构，请用 --arch' % args.fpk)
    ver = m.group(1) if m else os.path.basename(args.fpk).split('_')[1]
    arch = args.arch or m.group(2)

    prov = parse_provenance(args.provenance)
    changelog = args.changelog or prov.get('changelog') or (
        '同步上游 DBX v%s（commit %s，构建模式 %s）' % (
            ver, (prov.get('upstream_commit') or 'unknown')[:10], prov.get('build_mode') or 'release'))

    data = {}
    if os.path.exists(args.out):
        try:
            data = json.load(open(args.out, encoding='utf-8'))
        except Exception:
            data = {}
    data['schema_version'] = 2
    info = dict(SOURCE_INFO)
    info['homepage'] = args.repo_url
    data['source_info'] = info
    apps = data.setdefault('apps', {})
    app = apps.get(APP_KEY) or {}
    meta = dict(APP_META)
    meta['distributor_url'] = args.repo_url
    meta['bug_report_url'] = args.repo_url + '/issues'
    for k, v in meta.items():
        if k == 'platform':
            platforms = set(app.get('platform') or [])
            platforms.add(arch)
            app['platform'] = [p for p in ('x86', 'arm') if p in platforms] or [arch]
        else:
            app[k] = v
    releases = app.setdefault('releases', {})
    rel = releases.setdefault(ver, {'changelog': changelog, 'packages': {}})
    rel['changelog'] = changelog
    rel['packages'][arch] = {
        'download_url': args.download_url,
        'sha256': sha256_of(args.fpk),
        'size': os.path.getsize(args.fpk),
    }
    apps[APP_KEY] = app

    with open(args.out, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write('\n')
    print('[fnpack] 已更新 %s：%s %s → %s（%d 字节）' % (
        args.out, APP_KEY, ver, rel['packages'][arch]['download_url'], rel['packages'][arch]['size']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
