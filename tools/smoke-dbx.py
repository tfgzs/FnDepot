#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对装好的 DBX 做一次端到端功能体检（真实连库、真实查询）。

用法：python3 smoke-dbx.py [--base http://127.0.0.1:4224] [--password <登录密码>]
   * 首次运行走 /api/auth/setup 设置登录密码（默认随机，末尾打印）
   * 之后 /api/auth/login 登录
   * 建一条 MySQL 连接（默认本机共享库 127.0.0.1:13306）并执行 SELECT VERSION() / SHOW DATABASES
证据全部来自服务端真实返回，不做任何模拟。
"""
import argparse
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.request
import uuid

CRED = None      # 由 --credentials 指定（默认不做连库体检，避免依赖本机路径）


def mysql_root_password(path):
    """从共享库凭据文件里读 MySQL root 密码（没给文件就跳过连库测试）。"""
    if not path or not os.path.exists(path):
        return None
    txt = open(path, encoding='utf-8', errors='replace').read()
    m = re.search(r'超级管理员密码\s*:\s*(\S+)', txt)
    return m.group(1) if m else None


class Client:
    def __init__(self, base, password=None):
        self.base = base.rstrip('/')
        self.cookie = None
        self.password = password

    def call(self, method, path, body=None, timeout=60):
        req = urllib.request.Request(self.base + path,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     method=method)
        req.add_header('Content-Type', 'application/json')
        if self.cookie:
            req.add_header('Cookie', self.cookie)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode('utf-8', 'replace')
                sc = resp.headers.get('Set-Cookie')
                if sc and 'dbx_session=' in sc:
                    self.cookie = sc.split(';')[0]
                try:
                    return resp.status, json.loads(raw) if raw.strip() else {}
                except json.JSONDecodeError:
                    return resp.status, raw[:400]
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode('utf-8', 'replace')[:400]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='http://127.0.0.1:4224')
    ap.add_argument('--password', default=None)
    ap.add_argument('--credentials', default=CRED,
                    help='共享库凭据文件；给了才会做连库体检（可选）')
    ap.add_argument('--mysql-host', default='127.0.0.1')
    ap.add_argument('--mysql-port', type=int, default=13306)
    args = ap.parse_args()

    cli = Client(args.base)
    password = args.password or secrets.token_urlsafe(12)
    created = False
    print('[smoke] 本次登录密码: %s' % password, flush=True)

    st, body = cli.call('GET', '/api/auth/check')
    print('[smoke] auth/check ->', st, json.dumps(body, ensure_ascii=False))

    if isinstance(body, dict) and body.get('setup_required'):
        st, body = cli.call('POST', '/api/auth/setup', {'password': password})
        print('[smoke] auth/setup ->', st, json.dumps(body, ensure_ascii=False)[:200])
        created = True
    if cli.cookie is None:
        st, body = cli.call('POST', '/api/auth/login', {'password': password})
        print('[smoke] auth/login ->', st, json.dumps(body, ensure_ascii=False)[:200])

    st, body = cli.call('GET', '/api/auth/check')
    print('[smoke] auth/check(登录后) ->', st, json.dumps(body, ensure_ascii=False))

    dbrt = mysql_root_password(args.credentials)
    if not dbrt:
        print('[smoke] 找不到 MySQL 凭据，跳过连库测试')
        return 0

    cfg = {
        'id': str(uuid.uuid4()),
        'name': 'smoke-mysql-local',
        'db_type': 'mysql',
        'host': args.mysql_host,
        'port': args.mysql_port,
        'username': 'root',
        'password': dbrt,
        'database': 'mysql',
    }
    st, body = cli.call('POST', '/api/connection/test', {'config': cfg})
    print('[smoke] connection/test ->', st, json.dumps(body, ensure_ascii=False)[:300])

    # ⚠️ /api/connection/save 是【全量覆盖】：先把现有连接整份备份，体检完原样写回，
    #    否则会把用户已有的连接全部顶掉（踩过）。
    st, lst = cli.call('GET', '/api/connection/list')
    baseline = lst if isinstance(lst, list) else (lst.get('configs') or [])
    print('[smoke] 现有连接 %d 条（体检后原样还原）' % len(baseline))

    st, body = cli.call('POST', '/api/connection/save', {'configs': baseline + [cfg]})
    print('[smoke] connection/save ->', st, json.dumps(body, ensure_ascii=False)[:300])

    st, lst = cli.call('GET', '/api/connection/list')
    conns = lst if isinstance(lst, list) else (lst.get('configs') or lst.get('data') or [])
    target = None
    for c in conns:
        if isinstance(c, dict) and c.get('name') == cfg['name']:
            target = c
    if not target:
        print('[smoke] 连接列表里没找到刚保存的连接：', json.dumps(lst, ensure_ascii=False)[:300])
        return 1
    cid = target.get('id') or target.get('connectionId')
    print('[smoke] 连接 id =', cid, '| 列表是否回显密码：', bool(target.get('password')))

    st, body = cli.call('POST', '/api/connection/connect', {'config': dict(cfg, id=cid)})
    print('[smoke] connection/connect ->', st, json.dumps(body, ensure_ascii=False)[:300])

    st, body = cli.call('POST', '/api/connection/check-health', {'connectionId': cid})
    print('[smoke] connection/check-health ->', st, json.dumps(body, ensure_ascii=False)[:300])

    st, body = cli.call('GET', '/api/schema/databases?connection_id=%s' % cid)
    print('[smoke] schema/databases ->', st, json.dumps(body, ensure_ascii=False)[:300])

    for sql in ('SELECT VERSION() AS v', 'SHOW DATABASES'):
        st, body = cli.call('POST', '/api/query/execute',
                            {'connectionId': cid, 'database': 'mysql', 'sql': sql})
        print('[smoke] query/execute %-20s -> %s %s' % (sql, st, json.dumps(body, ensure_ascii=False)[:300]))

    # 还原：把体检用的临时连接从列表里摘掉，用户的连接一条不动
    st, _ = cli.call('POST', '/api/connection/save', {'configs': baseline})
    print('[smoke] 已还原连接列表（%d 条）' % len(baseline))

    print('[smoke] 登录密码（本次体检设置/使用）: %s' % password)
    if created:
        print('[smoke] 注意：本次是首次设置密码（setup）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
