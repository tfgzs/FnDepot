#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""免 root 在飞牛 fnOS 上跑一次性容器（走 appcgi.dockermgr 的 rpc）。

背景：agent / 普通账号访问不了 /var/run/docker.sock，飞牛的 Docker 由自己的
dockermgr 服务代理，所以在飞牛上做「编译」这类需要在容器里跑的事情时，用它的 rpc：

    fnosdocker.py run --image rust:1.97.1-bookworm --name dbx-build-rust \\
        --mount "$PWD:/work" \\
        --bash /work/docker/step-rust.sh --env MODE=source --timeout 5400

要点（都是实测踩出来的）：
  * rpc 的 `cmd` 必须是 **数组**，而且数组里每个元素都不能含空格
    （含空格的字符串会被再切一次）→ 所以真正干活的东西放在**主机上的脚本**里，
    容器里只跑 `bash /work/xxx.sh` 这种「无空格」命令。
  * `env` 是 ["K=V"] 字符串数组（写成 [{key,value}] 会直接 errno 52428800 报错）。
  * 容器默认以 root 运行；绑定挂载是真实挂载（宿主机目录直接可见），
    因此脚本结尾要用 `chown -R <属主> /work/out` 把产物交还给 NAS 属主。
  * 容器日志 rpc 没有暴露（只有 terminal 型 exec），所以脚本自己把输出写进挂载目录里的日志文件。
"""
import argparse
import importlib.util
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import localcfg      # noqa: E402  本机配置（local.cfg / 环境变量），公开代码里不写本机路径


def find_cfg():
    """fnOS 会话目录：环境变量 → local.cfg → ~/.trim-cli-* → ~/.trim-cli。"""
    d = localcfg.session_dir()
    if os.path.exists(os.path.join(d, 'session.json')):
        return d
    raise SystemExit('找不到飞牛会话（%s/session.json）：用 TRIM_CLI_CONFIG_DIR 指定，'
                     '或在项目根 local.cfg 里配 trim_cli_config_dir' % d)


def _load_raw():
    """复用 trim-raw.py（trim-cli 技能里的 WS 客户端）。"""
    cands = [os.path.join(HERE, 'trim-raw.py')]
    try:
        cands.append(os.path.join(localcfg.session_dir(), '..', 'trim-raw.py'))
        cands.append(os.path.join(localcfg.load().get('trim_raw_dir') or '', 'trim-raw.py'))
    except Exception:
        pass
    for cand in cands:
        if os.path.exists(cand):
            spec = importlib.util.spec_from_file_location('trim_raw', cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise SystemExit('找不到 trim-raw.py：放到 lib/ 下，或在 local.cfg 里配 trim_raw_dir')


class Rpc:
    def __init__(self, cfg=None):
        self.tr = _load_raw()
        self.cfg = cfg or find_cfg()
        self._reload_session()
        self._last_auth = 0.0

    def _reload_session(self):
        sess = json.load(open(os.path.join(self.cfg, 'session.json')))
        self.secret = self.tr.base64.b64decode(sess['secret']) if sess.get('secret') else b''
        self.token = sess['token']
        self.uid = sess.get('uid')
        self.long_token = sess.get('longToken')

    def refresh_token(self):
        """用 longToken 换新 token 并写回 session.json（免 2FA）。

        为什么需要：飞牛的 token 很短命，而一次编译要几十分钟 —— 不自动续期的话，
        构建跑到一半 rpc 就 401 了（实测踩过）。
        """
        if not self.long_token:
            return False
        ws = self.tr.Ws()
        try:
            r0 = self.tr.call(ws, self.secret, {'req': 'util.getSI'}, sign=False, quiet=True)
            si = r0.get('si') if isinstance(r0, dict) else None
            body = {'req': 'user.tokenLogin', 'token': self.long_token}
            if self.uid:
                body['uid'] = self.uid
            if si:
                body['si'] = si
            r1 = self.tr.call(ws, self.secret, body, sign=True, quiet=True)
            tok = r1.get('token') if isinstance(r1, dict) else None
            if not tok:
                return False
            path = os.path.join(self.cfg, 'session.json')
            sess = json.load(open(path))
            sess['token'] = tok
            json.dump(sess, open(path, 'w'))
            self.token = tok
            print('[fnosdocker] 会话已自动续期', flush=True)
            return True
        finally:
            ws.close()

    def _auth(self, ws, si):
        tr = self.tr
        base = {'token': self.token}
        if self.uid:
            base['uid'] = self.uid
        auth = tr.call(ws, self.secret,
                       dict({'req': 'user.authToken', 'main': True}, **base,
                            **({'si': si} if si else {})),
                       sign=True, quiet=True)
        return auth

    def call(self, req, fields=None, _retried=False):
        tr = self.tr
        # token 命短：距上次成功鉴权超过 12 分钟就先主动续期（长构建期间靠这个续命）
        if time.time() - self._last_auth > 720:
            self.refresh_token()
        ws = tr.Ws()
        try:
            si = tr.call(ws, self.secret, {'req': 'util.getSI'}, sign=False, quiet=True).get('si')
            auth = self._auth(ws, si)
            if auth.get('result') != 'succ':
                if not _retried and self.refresh_token():
                    ws.close()
                    return self.call(req, fields, _retried=True)
                raise SystemExit('飞牛会话已失效且自动续期失败（errno=%s）：请人工跑 本机会话续期脚本'
                                 % auth.get('errno'))
            self._last_auth = time.time()
            body = dict({'token': self.token}, **(fields or {}))
            if self.uid:
                body['uid'] = self.uid
            body['req'] = req
            if si:
                body['si'] = si
            return tr.call(ws, self.secret, body, sign=True, quiet=True)
        finally:
            ws.close()


def cmd_run(args):
    rpc = Rpc()
    mounts = [{'source': m.split(':')[0], 'target': m.split(':')[1],
               'perm': m.split(':')[2] if len(m.split(':')) > 2 else 'rw'} for m in args.mount]

    # 先清掉同名残留容器
    rpc.call('appcgi.dockermgr.containerStop', {'containerId': args.name})
    rpc.call('appcgi.dockermgr.containerRemove', {'containerId': args.name})

    payload = {
        'name': args.name,
        'image': args.image,
        'cmd': ['bash', args.bash],          # ★ 无空格，别改
        'env': args.env or [],
        'mount': mounts,
        'networks': [],
        'capAdd': [],
        'capDrop': [],
        'port': [],
        'link': [],
        'restart': False,
        'privileged': False,
        'gpuEnabled': False,
        'startWhenCreated': True,
    }
    if args.memory:
        # ★ 单位是**字节**（飞牛前端也是 memory(MB)*1024*1024 再传）；传 MB 会被判非法 → errno 52428800
        payload['memory'] = args.memory * 1024 * 1024

    res = rpc.call('appcgi.dockermgr.containerCreate', payload)
    if res.get('result') != 'succ':
        raise SystemExit('容器创建失败：%s' % json.dumps(res, ensure_ascii=False))
    cid = res['rsp']['Id']
    print('[fnosdocker] 容器已启动 %s (%s) image=%s' % (args.name, cid[:12], args.image), flush=True)

    deadline = time.time() + args.timeout
    state = {}
    while time.time() < deadline:
        try:
            state = rpc.call('appcgi.dockermgr.containerInspect', {'containerId': cid})['rsp']['State']
        except Exception as exc:                                    # noqa: BLE001
            print('[fnosdocker] inspect 失败（重试）：%s' % exc, flush=True)
            time.sleep(5)
            continue
        if not state.get('Running'):
            break
        time.sleep(args.poll)
    else:
        print('[fnosdocker] 超时 %ss，停止容器' % args.timeout, flush=True)
        rpc.call('appcgi.dockermgr.containerStop', {'containerId': cid})
        return 124

    code = int(state.get('ExitCode', -1))
    print('[fnosdocker] 容器结束 exit=%s oom=%s' % (code, state.get('OOMKilled')), flush=True)

    if args.keep_on_failure and code != 0:
        print('[fnosdocker] 保留容器便于排查：%s' % args.name)
    else:
        rpc.call('appcgi.dockermgr.containerRemove', {'containerId': cid})
    return code


def main():
    ap = argparse.ArgumentParser(description='免 root 跑一次性容器（fnOS dockermgr rpc）')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('run')
    p.add_argument('--image', required=True)
    p.add_argument('--name', required=True)
    p.add_argument('--bash', required=True, help='容器内的脚本路径（必须无空格）')
    p.add_argument('--mount', action='append', default=[], help='宿主机:容器[:rw]')
    p.add_argument('--env', action='append', default=[], help='K=V')
    p.add_argument('--memory', type=int, default=0, help='内存上限 MB（0=不限制）')
    p.add_argument('--timeout', type=int, default=3600)
    p.add_argument('--poll', type=int, default=10)
    p.add_argument('--keep-on-failure', action='store_true')
    p.set_defaults(func=cmd_run)
    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == '__main__':
    main()
