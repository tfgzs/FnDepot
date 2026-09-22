# DBX 飞牛版（fnOS）· 打包仓库 / FnDepot 分享源

把上游 [DBX](https://github.com/t8y2/dbx)（90+ 种数据库的一站式管理工具）打包成
**飞牛 fnOS 原生安装包（fpk）**：不依赖 Docker，装完在飞牛桌面上点图标就用，端口独立、可装卸。

本仓库同时是一个 **FnDepot 第三方源**：在飞牛的「应用商店 → FnDepot → 应用源」里添加
下面的地址，就能直接安装/升级。

```
https://raw.githubusercontent.com/tfgzs/FnDepot/main/fnpack.json
```

---

## 一、普通用户：怎么装

1. 飞牛 NAS →「应用商店」→ 打开 **FnDepot** → 「应用源」页面
2. 添加源，地址填：`https://raw.githubusercontent.com/tfgzs/FnDepot/main/fnpack.json`
3. 回到应用列表，找「**DBX 数据库管理**」→ 安装（首次会让你设一个登录密码）
4. 桌面 → 点图标（默认端口 **4224**，可在「应用设置」里改）

> 国内网络访问 GitHub 不稳时，可在 FnDepot 的「外部源网络配置」里给商店配一个代理。

---

## 二、打包者：三条命令

```bash
bash build.sh --mode release      # 取上游官方静态产物（校验 sha256 + ELF 静态性）→ 出 fpk（约 1 分钟）
bash build.sh --mode source       # 从上游源码自己编译（容器内，30-90 分钟）
bash build.sh --check             # 只看上游有没有新版本：退出码 10=已是最新，0=有新版
```

产物在 `dist/`：`dbx_<版本>_<x86|arm>.fpk` + `PROVENANCE-<版本>-<架构>.txt`（溯源清单：
上游 tag/commit、构建模式、二进制与 fpk 的 sha256、构建时间）。

常用开关：

| 开关 | 说明 |
| --- | --- |
| `--version v0.6.19` | 指定上游版本（默认取上游最新 release） |
| `--arch arm64` | 出 ARM 飞牛包（用上游 arm64 静态产物，平台字段自动变 `arm`） |
| `--packer native\|fnpack\|auto` | 打包器：原生 Python / 飞牛 fnpack / 自动（默认） |
| `--no-package` | 只准备二进制与前端，不打包 |
| `--skip-build` | 复用 `build/work/out/<版本>` 里已有的产物，只重新打包 |

### 环境要求

* Linux x86_64（飞牛 NAS、普通发行版、GitHub Actions 都行）
* `bash` `curl` `tar` `python3`（标准库即可，无第三方依赖）
* 打包：默认用自带 `lib/fpkpack.py`，**不需要**飞牛工具；有 `fnpack` 时也可 `--packer fnpack`
* `--mode source` 额外需要 Docker（见下）

---

## 三、两种构建模式

| 模式 | 做什么 | 耗时 | 适用 |
| --- | --- | --- | --- |
| `release`（默认） | 下载上游官方 `browser-static` 产物 → 官方 sha256 校验 → ELF 全静态校验 → 装进 fpk | ~1 分钟 | 日常同步、CI、上架 |
| `source` | 容器内用上游 CI 同款参数自己编译：`cargo zigbuild --release -p dbx-web --target <arch>-unknown-linux-musl --no-default-features --features "duckdb-sidecar,dynamodb,mq-admin,dbx-core/sqlite-bundled"` + `pnpm build` | 30-90 分钟 | 想做可复现自编译、或要打补丁 |
| `auto` | 先 `source`，失败退回 `release` | — | 想尽量自编译、但编不出也要出包 |

### 3.1 为什么很少需要 `source`

上游发布物本身就是**全静态 musl 单文件**（无解释器、无动态库），跨发行版、跨飞牛版本都能跑；
本仓库对它做了**双重校验**（官方 sha256 + 自证 ELF 无 `PT_INTERP`/无 `DT_NEEDED`）后原样装入，
既省时间，也和上游 Docker 镜像里的运行时是同一份东西。

### 3.2 `--mode source` 的已知门槛（实测记录）

* **`RUST_MIN_STACK` 必须加大**：`crates/dbx-drivers` / `crates/dbx-core` 这类巨型 crate 会让 rustc
  深递归。默认 8MB 栈会 `rustc interrupted by SIGSEGV`，脚本里已设 `RUST_MIN_STACK=512MB`。
* **内存**：单并发 `DBX_CARGO_JOBS=1` + 容器 4GB 起步（脚本默认值）。
* **⚠️ 在飞牛上编译，cargo 的 target 目录不能放在数据卷里**：飞牛 `/vol1`~`/vol4` 都是
  `ext4+trimacl` 挂载，进程新建的文件权限位会被抹成 `000`，rustc 会直接报
  `output file ... is not writeable`。本仓库把缓存放在根文件系统（默认 `/tmp/dbx-cargo`，
  可用 `DBX_CARGO_CACHE_HOST` 改），并用 bind mount 挂进容器。
* **某些 rustc 版本可能有编译器侧问题**：例如 rustc 1.97.1 编译 `dbx-core` 时会出现
  「提示的栈大小逐档翻倍（128MB→256MB→512MB→1GB→2GB）」的无界递归，加栈也修不好。
  遇到这种就换更新的 rustc 再试：
  ```bash
  DBX_RUST_IMAGE=rust:1-bookworm DBX_SYNC_MODE=source bash build.sh --mode source
  ```

---

## 四、把这个源分享给别人（FnDepot）

```
fnpack.json          ← 源清单（schema_version 2），别人加源时就是拉这个文件
dbx/ICON.PNG         ← 商店列表图标（64x64）
dbx/README.md        ← 商店里「详情」页显示的应用说明
dbx/Preview/*.png    ← 可选的界面截图
```

源清单由工具生成，别手写：

```bash
python3 tools/gen-fnpack.py \
  --fpk dist/dbx_0.6.19_x86.fpk \
  --provenance dist/PROVENANCE-0.6.19-x64.txt \
  --download-url https://github.com/tfgzs/FnDepot/releases/download/v0.6.19/dbx_0.6.19_x86.fpk \
  --out fnpack.json
```

同一个版本重复跑是幂等的；换架构（x86/arm）会合并进同一个 release 条目。

### 自动发布（GitHub Actions）

`.github/workflows/release.yml`：每周一检查上游新版本（也可手动 Run workflow 或推 `v*` tag）→
在 CI 里 `--mode release --packer native` 出包 → 建 Release 传 fpk → 更新 `fnpack.json` 并提交。
**不需要任何密钥**（用内置 `GITHUB_TOKEN`），也不需要飞牛工具或 Docker。

---

## 五、仓库结构

```
build.sh                构建主流程（取产物 → 校验 → 组装 fpk → 自检 → 出包 → 写溯源）
sync.sh                 本机定时同步用（含本机账号/路径，未随仓库发布）
docker/step-frontend.sh source 模式：容器内 pnpm 构建前端
docker/step-backend.sh  source 模式：容器内 cargo zigbuild 编译静态 musl 后端
docker/lib-checkout.sh  共用：按 tag 检出上游源码
fpk/                    fpk 骨架（manifest 模板、cmd 生命周期脚本、config、wizard、ui、图标）
lib/fpkpack.py          原生 fpk 打包器（纯 Python，无 fnpack 依赖）
lib/fpkcheck.py         打包前后自检闸门（结构 / manifest / 图标尺寸 / 向导 JSON / 本机痕迹扫描）
lib/elfcheck.py         纯 Python ELF 检查（确认二进制全静态）
lib/fnosdocker.py       在飞牛上免 root 起构建容器（走 dockermgr rpc）
lib/png_resize.py       纯 Python PNG 缩放（生成 64/256 图标）
tools/gen-fnpack.py     生成 / 更新 FnDepot 源清单
tools/install-fpk.py    在本机飞牛上装包（走应用中心接口，支持向导参数）
tools/uninstall-dbx.py  卸载（可带 --purge 删数据）
tools/smoke-dbx.py      功能体检（登录 → 建连接 → 真跑查询 → 还原连接列表）
docs/local-nas.md       飞牛 NAS 上的特殊处理（没有 docker.sock 权限时怎么跑构建）
```

---

## 六、常见问题

**装完打不开 / 端口被占？** fpk 的 `checkport=true`，启动前会检查端口。改端口：应用中心 →
「DBX 数据库管理」→「设置」→ 服务端口（改完重启应用；桌面图标会跟着用新端口）。

**想升级但不想丢数据？** 升级走「卸载（保留数据）+ 安装」，连接、查询历史、登录密码都在
应用数据目录里，不会丢。数据目录：`/volN/@appdata/dbx/`。

**别人装的时候能从 GitHub 下到包吗？** 包放在本仓库的 Release 里；如果对方网络访问 GitHub 困难，
可以让他用 FnDepot 的代理设置，或者直接把 `dist/*.fpk` 发给他在应用中心「手动安装」。

**能不能上架官方「飞牛市场」？** 可以另行走官方上架流程；本仓库的 `manifest` 字段已按官方规范填写
（`distributor` 换成你自己的身份即可）。

---

## 七、许可

* 打包脚本与文档：MIT（见 LICENSE）
* 分发的应用本体：上游 DBX，Apache-2.0（见 NOTICE）
