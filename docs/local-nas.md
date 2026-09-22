# 在飞牛 NAS 上跑这套构建（特殊处理）

普通 Linux 上按 README 直接跑即可。飞牛（fnOS）上有几处需要绕的坑，这里记录实测结论。

## 1. 没有 docker.sock 权限怎么起构建容器

飞牛上普通账号（含管理员）通常不在 `docker` 组里，`docker` 命令直接 Permission denied。
但飞牛的 Docker 是自己的服务在管，可以用它的 rpc 起容器 —— `lib/fnosdocker.py` 就是干这个的：

```bash
# 需要一份 fnOS 会话（token），取自 trim-cli 的会话文件
python3 lib/fnosdocker.py run \
  --image rust:1.97.1-bookworm \
  --name dbx-build \
  --mount "$PWD:/work" \
  --bash /work/docker/step-backend.sh \
  --env TAG=v0.6.19 --env WORK=/work/build/work --env TARGET=x86_64-unknown-linux-musl \
  --memory 4096 --timeout 10800
```

实测要点（都是踩出来的）：

| 现象 | 原因 / 解法 |
| --- | --- |
| `errno 52428800` 创建容器失败 | `cmd` 必须是**数组**且元素**不含空格**；`env` 必须是 `["K=V"]`；`memory` 单位是**字节**（传 MB 会被判非法） |
| 想跑的复杂命令没法传 | 把逻辑写在**脚本文件**里（随 `--mount` 进去），只传 `["bash","/work/xxx.sh"]` |
| 看不到容器日志 | rpc 没有 logs 接口 → 让脚本把输出写进挂载目录里的文件，宿主侧再读 |
| 长时间构建中途 401 | fnOS 的会话 token 只有十几分钟寿命 → `fnosdocker.py` 会**自动续期**（用 longToken 换新并写回会话文件） |
| 容器写出来的文件宿主改不了 | 容器里是 root → 脚本收尾 `chown -R <宿主uid>:<宿主gid> /work/...` |
| 拿不到容器退出码 | 轮询 `containerInspect` 的 `State.ExitCode`（脚本已封装） |

## 2. 数据卷是 `trimacl`，Rust 编译不能把 target 放进去

飞牛的 `/vol1`~`/vol4` 挂载参数是 `rw,relatime,trimacl,prjquota`：**进程新建的文件权限位会被抹成 `000`**。

* 对 node/vite 无害（它不检查权限位，且我们随后会 `chmod` 补执行位）；
* 对 **rustc 致命**：它会先 `stat` 输出文件、发现不可写就报
  `error: output file ... is not writeable`，直接编译失败（同一个 crate 换到无 trimacl 的路径就正常，已最小化复现）。

所以 cargo 的 `CARGO_TARGET_DIR` / `CARGO_HOME` 放在**根文件系统**（默认 `/tmp/dbx-cargo`，
可用 `DBX_CARGO_CACHE_HOST` 覆盖），再 bind mount 进容器；根分区空间不足会自动清缓存
（`DBX_CARGO_CACHE_MIN_FREE_GB` 控制阈值，默认 12GB）。

## 3. 打包：能不用 fnpack 就不用

`fnpack` 是飞牛自带工具（静态 ELF，只有飞牛上有）。本仓库自带 `lib/fpkpack.py`，
纯 Python 复刻了它的产物格式，所以：

* 在飞牛上：`--packer auto`（默认）会优先用 fnpack，没有就用原生；
* 在别的 Linux / CI 上：直接原生打包，无需任何飞牛工具。

fpk 的格式（实测对照 fnpack 1.2.4 产物）：

```
fpk (tar.gz)
├── manifest              key = value 文本，最后一行是 checksum = md5(app.tgz)
├── cmd/                  生命周期脚本：main + install/config/upgrade/uninstall 的 init/callback
├── config/               privilege（run-as 等）/ resource
├── wizard/               安装/配置/卸载向导 JSON
├── ICON.PNG  ICON_256.PNG  64x64 / 256x256
└── app.tgz              应用本体：app/ 的内容 **加一份 config/**（没有 app/ 前缀）
```

## 4. 安装 / 卸载本机验证

```bash
python3 tools/install-fpk.py dist/dbx_0.6.19_x86.fpk --port 4224   # 走应用中心接口，支持向导参数
python3 tools/uninstall-dbx.py --purge                            # 卸载并删数据
python3 tools/smoke-dbx.py --password <登录密码> --credentials <共享库凭据文件>
```

* 带 `wizard/` 的包用 CLI（`app install-fpk`）会被拒，必须走应用中心的 HTTP 接口 —— `tools/install-fpk.py` 封装了这条链路。
* `install-fpk.py` 重装时是「卸载（保留数据）+ 安装」，等价于升级，不丢连接与密码。
* `smoke-dbx.py` 做真查询体检；注意 `/api/connection/save` 是**全量覆盖**语义，脚本已做「先备份、测完还原」。
