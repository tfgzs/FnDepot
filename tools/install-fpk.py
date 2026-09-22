#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把带向导（wizard）的 fpk 装到本机飞牛：走应用中心的 HTTP 接口。

为什么不用 `trim-cli app install-fpk`：带 wizard/install 的包，CLI 会直接拒绝
（"install requires custom wizard parameters"）。应用中心前端「手动安装」走的就是下面这套接口：

    POST /app-center/v1/download/task   {"packageSourceType":"file","path":"<绝对路径>"}
    GET  /app-center/v1/download/status?downloadTaskId=...
    GET  /app-center/v1/install/info?version=&appName=&packageType=path&language=zh-CN
    POST /app-center/v1/install/task    {appName,version,packageType,systemParameters,customParameters}
    POST /app-center/v1/install/status  {"taskId":...}

用法：
    install-fpk.py <file.fpk> [--port 4224] [--volume 1] [--reinstall] [--keep-data] [--uninstall]

注意：path 必须是**绝对路径**；已安装的应用必须先卸载（10236）。
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


def token():
    path = os.path.join(localcfg.session_dir(), 'session.json')
    if not os.path.exists(path):
        sys.exit('找不到飞牛会话：%s（先给本机 fnOS 会话续期，或在 local.cfg 里配 trim_cli_config_dir）' % path)
    return json.load(open(path))['token']


class Api:
    def __init__(self):
        self.tok = token()

    def call(self, method, path, body=None, query=None):
        url = BASE + path + (('?' + urllib.parse.urlencode(query)) if query else '')
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        # 应用中心的接口用 Cookie: fnos-token=<token> 鉴权（实测加 Authorization: Bearer 反而会被判 401）
        req.add_header('Cookie', 'fnos-token=%s' % self.tok)
        req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode('utf-8', 'replace') or '{}')
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode('utf-8', 'replace')
            sys.exit('HTTP %s %s -> %s' % (exc.code, path, raw[:400]))

    def installed(self, app):
        r = self.call('GET', '/app-center/v1/app/installed/base-info', query={'language': 'zh-CN'})
        data = r.get('data') if isinstance(r.get('data'), list) else (r if isinstance(r, list) else [])
        for item in data:
            if item.get('appName') == app:
                return item
        return None


def uninstall(api, app, keep_data=True):
    info = api.call('GET', '/app-center/v1/uninstall/info', query={'appName': app})
    fields = [(it.get('field')) for st in ((info.get('data') or {}).get('wizardInfo') or {}).get('wizardContent', [])
              for it in st.get('items', [])] if info.get('data') else []
    params = []
    if 'wizard_delete_data' in fields:
        params.append({'key': 'wizard_delete_data', 'value': 'false' if keep_data else 'true'})
    print('[install] 卸载已安装的 %s（保留数据=%s）' % (app, keep_data))
    r = api.call('POST', '/app-center/v1/uninstall/start', {'appName': app, 'customParameters': params})
    if r.get('code') not in (0, None):
        print('[install] 卸载返回：%s' % json.dumps(r, ensure_ascii=False)[:300])
    for _ in range(60):
        time.sleep(5)
        if not api.installed(app):
            print('[install] 卸载完成')
            return
    sys.exit('卸载超时，请到应用中心确认状态后重试')


def install(api, fpk, port, volume, keep_data):
    fpk = os.path.abspath(fpk)
    if not os.path.isfile(fpk):
        sys.exit('找不到 fpk：%s' % fpk)

    r = api.call('POST', '/app-center/v1/download/task',
                 {'packageSourceType': 'file', 'path': fpk})
    task_id = (r.get('data') or {}).get('downloadTaskId')
    if not task_id:
        sys.exit('登记 fpk 失败：%s' % json.dumps(r, ensure_ascii=False)[:400])
    print('[install] 解析包：%s' % fpk)

    app = version = None
    for _ in range(120):
        time.sleep(2)
        st = api.call('GET', '/app-center/v1/download/status', query={'downloadTaskId': task_id})
        data = st.get('data') or {}
        if data.get('status') == 2:
            app, version = data.get('appName'), data.get('version')
            break
        if data.get('status') == 3:
            sys.exit('包解析失败：%s' % json.dumps(st, ensure_ascii=False)[:400])
    if not app:
        sys.exit('解析包超时')
    print('[install] 包信息：app=%s version=%s' % (app, version))

    if api.installed(app):
        uninstall(api, app, keep_data=keep_data)

    info = api.call('GET', '/app-center/v1/install/info',
                    query={'version': version, 'appName': app, 'packageType': 'path', 'language': 'zh-CN'})
    wiz = (info.get('data') or {}).get('wizardInfo') or {}
    fields = [it.get('field') for st in (wiz.get('wizardContent') or []) for it in st.get('items', [])]
    print('[install] 向导字段：%s' % (fields or '(无)'))

    custom = []
    if 'wizard_port' in fields:
        custom.append({'key': 'wizard_port', 'value': str(port)})

    body = {
        'appName': app, 'version': version, 'packageType': 'path',
        'systemParameters': {
            'agreedToProtocol': True,
            'installVolumeID': volume,
            'dataVolumeId': volume,
            'immediateStart': True,
            'apiScope': {},
        },
        'customParameters': custom,
    }
    r = api.call('POST', '/app-center/v1/install/task', body)
    if r.get('code') not in (0, None):
        sys.exit('安装请求失败：%s' % json.dumps(r, ensure_ascii=False)[:400])
    task = (r.get('data') or {}).get('taskId')
    print('[install] 安装中（taskId=%s）…' % task)

    for _ in range(180):
        time.sleep(5)
        st = api.call('POST', '/app-center/v1/install/status', {'taskId': task})
        d = st.get('data') or {}
        if d.get('status') == 2 and d.get('progress', 0) >= 100:
            print('[install] ✅ 安装完成：%s %s' % (app, version))
            return 0
        if d.get('status') == 3:
            sys.exit('安装失败：%s' % json.dumps(st, ensure_ascii=False)[:400])
    sys.exit('安装超时')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('fpk')
    ap.add_argument('--port', default='4224', help='向导里的服务端口')
    ap.add_argument('--volume', type=int, default=1, help='安装到哪个存储卷（1 = /vol1）')
    ap.add_argument('--reinstall', action='store_true', help='已安装则先卸载再装')
    ap.add_argument('--keep-data', action='store_true', default=True)
    ap.add_argument('--uninstall', action='store_true', help='只卸载不安装')
    args = ap.parse_args()

    api = Api()
    if args.uninstall:
        uninstall(api, 'dbx', keep_data=args.keep_data)
        return 0
    if args.reinstall:
        if api.installed('dbx'):
            uninstall(api, 'dbx', keep_data=True)
    return install(api, args.fpk, args.port, args.volume, args.keep_data)


if __name__ == '__main__':
    sys.exit(main())
