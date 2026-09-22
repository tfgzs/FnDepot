#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""卸载本机飞牛上的 DBX（可带向导参数选择是否删数据）。

用法：uninstall-dbx.py [--purge] [--app dbx]
  --purge  同时删除应用数据目录（向导 wizard_delete_data=true）
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get('FNOS_API', 'http://127.0.0.1:5666')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'lib'))
import localcfg      # 本机会话目录（local.cfg / 环境变量），公开代码里不写死路径


def api(method, path, body=None, query=None):
    tok = json.load(open(os.path.join(localcfg.session_dir(), 'session.json')))['token']
    url = BASE + path + (('?' + urllib.parse.urlencode(query)) if query else '')
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method)
    req.add_header('Cookie', 'fnos-token=%s' % tok)
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode('utf-8', 'replace') or '{}')
    except urllib.error.HTTPError as exc:
        sys.exit('HTTP %s %s -> %s' % (exc.code, path, exc.read().decode('utf-8', 'replace')[:300]))


def installed(app):
    r = api('GET', '/app-center/v1/app/installed/base-info', query={'language': 'zh-CN'})
    data = r.get('data') if isinstance(r.get('data'), list) else (r if isinstance(r, list) else [])
    return [a for a in data if a.get('appName') == app]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--app', default='dbx')
    ap.add_argument('--purge', action='store_true', help='同时删除应用数据目录')
    args = ap.parse_args()

    if not installed(args.app):
        print('[uninstall] %s 未安装，无需卸载' % args.app)
        return 0

    info = api('GET', '/app-center/v1/uninstall/info', query={'appName': args.app, 'language': 'zh-CN'})
    wiz = ((info.get('data') or {}).get('wizardInfo') or {})
    fields = [it.get('field') for st in (wiz.get('wizardContent') or []) for it in st.get('items', [])]
    params = []
    if 'wizard_delete_data' in fields:
        params.append({'key': 'wizard_delete_data', 'value': 'true' if args.purge else 'false'})
    print('[uninstall] 卸载 %s（删除数据=%s，向导字段=%s）' % (args.app, args.purge, fields))

    r = api('POST', '/app-center/v1/uninstall/start', {'appName': args.app, 'customParameters': params})
    if r.get('code') not in (0, None):
        print('[uninstall] 返回：%s' % json.dumps(r, ensure_ascii=False)[:300])

    for _ in range(60):
        time.sleep(5)
        if not installed(args.app):
            print('[uninstall] ✅ 已卸载')
            return 0
    sys.exit('[uninstall] 超时，请到应用中心确认状态')


if __name__ == '__main__':
    sys.exit(main())
