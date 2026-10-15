# 用 LuZ 基础镜像包复原 Switch 镜像

本目录发布**增量层与校验工具**，完整镜像包另行存放，不上传 GitHub。推荐走下述“基础包 + 增量”路径，免去重新编译依赖。

## 已核实的差异

| 对象 | 实测镜像相对 LuZ `0.3.1-r3` 的差异 |
|---|---|
| 基础 RootFS | 原来的 71 个层完整保留，顺序和 diff ID 一致 |
| 新增镜像层 | 5 个，压缩 blob 合计 **5,408,326 bytes**；解压后的层 tar 合计 **28,502,528 bytes** |
| Docker 报告的大小 | 基础 14,439,691,572 bytes；实测 14,445,102,976 bytes，增加 5,411,404 bytes，约 **0.0375%** |
| 镜像构建源码 | 915 个跟踪输入文件中，仅 `_roce_proxy.c` 与发布时的原仓库输入不同；另有安装/打包脚本 |
| 当前 B20 运行补丁 | 五个文件通过只读 bind mount 覆盖，**不在 `docker save` 中** |

Docker/OCI 压缩大小、解压后层 tar 大小、最终文件系统大小不是一个指标。新层把已有项目源码复制进基础镜像，所以源码数量不等于独立修改数量。不能由 0.04% 推断性能影响也只有 0.04%。

当前 B20 的 KV pool、FP4 indexer 分支修正、V4 extra-KV page64、WO-A、L2 预取和 RoCE Python 路由等，还依赖 [overrides](../overrides/manifest.json) 中的五个 B20 文件及四节点配置。**仅加载镜像不等于完成 B20 部署。**

## 镜像身份与脱敏

不要把 OCI index digest、平台 manifest digest、image config digest 混写为一个 SHA。此环境使用 Docker containerd image store，`docker image inspect .Id` 返回的是 index digest；其他 Docker 存储后端可能返回 config digest。

| 身份 | 摘要 |
|---|---|
| 指定 LuZ 基础镜像 OCI index | `f8095fa8d6ed867a9b40c5a90dbadd942bf12d80443d0b8db76ae24a36e415c1` |
| 指定基础 image config | `a23298ed93e0dc5042e7b9634e705cbeddee849821aeb7322b8c997a92394e93` |
| 性能测试时的镜像 OCI index | `3aca5abc11376e522c0c72852d2cea3030eab11c45c2555544ce8f21632b894a` |
| 性能测试时的 image config | `f65b8a8806b99dcc1458433b845b64af342227c6197c8a064e585c12e3e4fdd2` |
| 本目录脱敏镜像 OCI index | `d2d66bb82dc9b09780b9d791ae261b8e204239d7c077178a39af8aa6ab7f329c` |
| 本目录脱敏 image config | `ab4036ef55963003f054eb318a291174551051dfd4eb7e176149a729410bf092` |

所有摘要的算法均为 SHA256。完整机器可读描述见 [delta.json](delta.json)，每个新增 blob 都按内容 SHA256 命名。

公开版与实测镜像有三项分发处理：

1. 删除包含原始内网 registry 地址的 BuildKit provenance/attestation，以及原始 tag/registry annotations。
2. 将 `/opt/dsv41/adapter/lm_cert.py` **模块说明文字**中的上游内网镜像地址改成示例域名。去掉模块 docstring 后的 Python AST 完全一致；其余该层文件内容保持原样。
3. 追加一个很小的清理层，移除最终文件系统中的六个预生成 SSH host key/key.pub 文件。因此公开版共 **77 层 = 71 个基础层 + 5 个改造层 + 1 个分发清理层**。

增量目录的 9 个 blob 合计 **5,465,608 bytes（约 5.21 MiB）**。它不含模型、Engram、个人 SSH 密钥、运行配置、API key 或基础镜像层。

基础镜像扫描命中的密钥标记大多来自解析器和测试数据；另确认基础层含 `/etc/ssh/ssh_host_*_key` 预生成密钥。上述 whiteout 使它们不再出现在最终容器文件系统中，**无法擦除旧基础层中的历史字节**。基础包由使用者自行取得，GitHub 增量没有再次分发这些基础层或密钥。若需要基础层历史也彻底清理，必须另做文件系统扁平化或重建基础镜像，并重新验收；本增量不作这种声明。

本次验证覆盖逐 blob 校验、完整归档重组、真实 Docker load、CPU Python 容器、RootFS 身份和清理结果。**没有用脱敏镜像重新跑 GPU 性能/正确性矩阵**；既有测速仍对应原实测 index。分发改动不涉及计算代码，不据此冒充新的线上性能验收。

## 1. 准备正确的基础包

使用原项目的 **`dsv41-sglang-optimized-0.3.1-r3.tar` OCI 格式包**。不能拿另一个同名 tag、其他版本、其他架构或更大的新主线镜像替代。校验工具检查实际 config 和所有层内容，不只检查文件名。

先保留下载渠道给出的校验值，并按原项目说明校验。基础包如另经 zstd 压缩，先解开外层压缩；不要解开内部 `blobs/sha256/` 的 gzip 层：

```bash
zstd -dc dsv41-sglang-optimized-0.3.1-r3.tar.zst > /srv/images/luz-base-r3.tar
```

若拿到的本来就是 `.tar`，直接用它。工具要求标准库 Python 3.10+，本步骤不需要 PyTorch、GPU、Docker daemon、联网或安装 pip 包。

## 2. 先校验基础层与增量

在仓库根目录执行：

```bash
(cd switch/image-delta && sha256sum -c SHA256SUMS)
python3 switch/tools/assemble_image.py \
  --base /srv/images/dsv41-sglang-optimized-0.3.1-r3.tar \
  --verify-only
```

工具验证基础 config SHA、71 层 diff ID 序列、每个所需压缩层的内容 SHA，以及新层解压后的 diff ID。它不会启动服务或将 tar 中的路径解压到主机。`--verify-only` 仍需读取约 14.4GB 数据，不应与测速同时运行。

若提示缺失基础 config 或 layer representation 不同，说明包不匹配。旧 Docker storage backend 重新 `docker save` 可能改成另一种层表示，即使最终文件系统相同也不满足本增量格式。使用指定的原始 OCI 包；不要跳过检查，也不要手改摘要。

## 3. 重组并导入完整镜像

**增量目录不能直接 `docker load`**。必须把本地基础层与公开增量重组成完整 save archive：

```bash
python3 switch/tools/assemble_image.py \
  --base /srv/images/dsv41-sglang-optimized-0.3.1-r3.tar \
  --output /srv/images/luz-switch-public-20261015.tar
docker load -i /srv/images/luz-switch-public-20261015.tar
docker image inspect --format '{{.Id}}' luz-switch:20261015
```

输出文件不能已存在。失败时 `.partial` 保留供诊断，不视为可导入成品。原始 base 不被修改。每次读取用于输出的内容时都会校验 SHA。tar header 和公开 tag 已重新生成，不要求输出 tar 与历史 `docker save` 的整体 SHA 相同。

空间有限时可将完整 tar 直接传给 Docker，避免中间的 14.4GB 文件（Bash 必须保留管道错误）：

```bash
set -o pipefail
python3 switch/tools/assemble_image.py \
  --base /srv/images/dsv41-sglang-optimized-0.3.1-r3.tar --output - | docker load
```

进度/校验回执写 stderr，tar 写 stdout。不要把 `2>&1` 混进 tar 流。建议首次仍用落盘方式，便于复核和分发。

## 4. 检查镜像内容

```bash
docker run --rm --network none --entrypoint python3 luz-switch:20261015 -c \
 'import pathlib; assert not list(pathlib.Path("/etc/ssh").glob("ssh_host_*")); print("host-key cleanup OK")'
docker image inspect --format '{{json .RootFS.Layers}}' luz-switch:20261015
```

RootFS 列表应与 `delta.json` 的 `public_diff_ids` 完全相同（77 个）。本机实际 `docker image inspect .Id` 的结果填入 `cluster.private.json` 的 `image_id`，与 `image` tag 一并配置，随后按 [部署手册](../DEPLOYMENT-GUIDE.zh-CN.md) 安装五个 B20 挂载、Engram 和四节点配置。不要把历史 `3aca...` 直接填给脱敏镜像。

## 5. 从源码重建的备选路径

若希望审阅和修改全部构建过程，先导入 LuZ 基础包，再将对应镜像打上 `luz-base:0.3.1-r3` 标签。在仓库根目录执行：

```bash
docker build --build-arg BASE_IMAGE=luz-base:0.3.1-r3 \
  -f switch/Dockerfile -t luz-switch:source-20261015 .
```

`switch/Dockerfile` 复用根目录的 `deploy/adapter`、`deploy/b12x` 和 `deploy/overlay/o12c1-prod`，只覆盖 `_roce_proxy.c`，安装部署脚本并进行分发清理。原目录文件不需改动。[source-manifest.json](../image-context/source-manifest.json) 记录 915 个输入文件，`verify_source.py` 可按清单核验。

源码构建受时间戳、tar 排序、构建器和编译产物影响，**不保证生成上述公开 index/config digest**。它与“复用二进制层的增量重组”是两个构建路径；实际部署均须记录新 ID 并重新做功能、长上下文和速度验收。

## 6. 单独分发完整包

本次另有 `luz-switch-public-b20-20261015.tar.zst`。大小和 SHA 见 [archive-manifest.json](../images/archive-manifest.json)。使用方式见 [images/README.md](../images/README.md)。GitHub 只包含这里约 5.5MB 的增量；完整包保持仓库外存放。
