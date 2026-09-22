#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机配置（不进仓库）。

公开代码里不写任何本机路径/账号；本机特有的东西放项目根目录的 `local.cfg`（已 gitignore），
或者用环境变量覆盖。查找优先级：环境变量 → local.cfg → 通用默认。

local.cfg 示例（JSON）：

    {
      "trim_cli_config_dir": "/path/to/session-dir",   # 放 session.json 的目录（trim-cli 约定）
      "ssh_user_host":       "user@127.0.0.1"          # 需要以本机某账号执行构建时用
    }
"""
import glob
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load():
    path = os.path.join(ROOT, 'local.cfg')
    if not os.path.exists(path):
        return {}
    try:
        return json.load(open(path, encoding='utf-8'))
    except Exception:
        return {}


def session_dir():
    """fnOS 会话目录（里面有 session.json）。"""
    env = os.environ.get('TRIM_CLI_CONFIG_DIR')
    if env:
        return env
    cfg = load().get('trim_cli_config_dir')
    if cfg and os.path.isdir(cfg):
        return cfg
    for cand in sorted(glob.glob(os.path.expanduser('~/.trim-cli-*'))) + [os.path.expanduser('~/.trim-cli')]:
        if os.path.exists(os.path.join(cand, 'session.json')):
            return cand
    return os.path.expanduser('~/.trim-cli')


def ssh_user_host():
    """需要以别的本机账号跑构建时用（如飞牛上没有 docker.sock 权限的账号）。"""
    return os.environ.get('DBX_TRIM_SSH') or load().get('ssh_user_host') or ''
