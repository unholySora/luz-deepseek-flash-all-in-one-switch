# 单独镜像包与增量

推荐使用 [约 5.5MB 的公开增量和复原手册](../image-delta/README.zh-CN.md)，配合指定的 LuZ `0.3.1-r3` 原始基础包构建。完整镜像、模型权重和机器凭据均排除 Git。

另行打包的完整归档为 `luz-switch-public-b20-20261015.tar.zst`，**14,204,924,951 bytes**。其 SHA256 为 `6f7aab2cf5c15ee3224cd4b418000629040d6f5920aa97e2484d4c4220124dfa`。详见 [archive-manifest.json](archive-manifest.json)。

拿到该包与本目录的 `SHA256SUMS` 后：

```bash
sha256sum -c SHA256SUMS
set -o pipefail
zstd -dc luz-switch-public-b20-20261015.tar.zst | docker load
docker image inspect --format '{{.Id}}' luz-switch:20261015
```

Docker containerd image store 下预期 index digest 为 `sha256:d2d66bb82dc9b09780b9d791ae261b8e204239d7c077178a39af8aa6ab7f329c`。其他存储后端可能返回 image config digest `sha256:ab4036ef55963003f054eb318a291174551051dfd4eb7e176149a729410bf092`；还需将 RootFS 列表与 `image-delta/delta.json` 对比。四节点须使用相同内容与身份，把本机实际 `.Id` 写入 cluster 配置。

性能测试镜像仍为历史 index `sha256:3aca5abc11376e522c0c72852d2cea3030eab11c45c2555544ce8f21632b894a`。公开版删除内网 provenance、替换一处模块 docstring，并追加 SSH host key 清理层，所以 digest 不同。Docker load、CPU 容器与层校验已通过；公开版未重新跑 GPU 性能矩阵。

分发对象不包含运行容器的模型、Engram、缓存、个人 SSH 或状态 bind mounts。**五个 B20 只读覆盖文件仍须单独挂载**。基础镜像历史层中的预生成 SSH host key 字节未被安全擦除，完整包也不声称对基础镜像做了彻底内容清洗；公开增量不重复分发这些基础层。
