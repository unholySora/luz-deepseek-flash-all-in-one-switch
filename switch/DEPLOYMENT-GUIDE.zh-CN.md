# LuZ Switch 极详细部署与验收手册

版本：`switch-v2026.10.15-b20`。适用对象：四台 DGX Spark / GB10，经交换机组成双 rail RoCE 集群。目标：每请求 **600000** 总上下文，最多 **16** 个运行请求，实际 **9600000 token** KV 池。

本手册把“准备环境、加载镜像、生成配置、启动成功、功能正确、速度可比较”分别给出操作和完成判据。发布目录为 `switch/`，原项目文件保留原样。真实地址、用户名、SSH 密钥、平台授权及运行日志由部署者在本地维护。

## 0. 阅读顺序和发布边界

首次部署依次执行第 1–13 节，再按第 14–18 节验收。已有同拓扑环境，可从第 4 节检查网络，再直接加载镜像和生成配置。

本次完成的成果：

- B20：V4 extra-KV、page64、cap0.15、prefill/decode 入场间隔 1；正式四场景 C1/C2/C4/C8 各三轮，180 条完整 2048-token 输出。
- structured v2：计数最大值随输出 token 预算变化；与旧的“数到 200 后强制扩写”分开归档。新结果和数字连续性检查见发布报告。
- B32：FP4 extra-KV 和 C8 hash repack 的独立省内存选项，包含数值、graph、有限业务验收。
- 单路 599440-token 三处召回与 C8 功能验收。
- 未做完整 C16 吞吐或 16 条长请求同时填满 9.6M 的验收。

`600000` 是服务的总上下文限制，输入、聊天模板和输出都需要占用上下文。`9600000` 是服务报告的 KV token 池上限；它不表示任意长度、任意输入输出组合都能无排队运行，也不等同于已经完成 16 路长文满载测试。

源码模板可移植；实测性能对应固定镜像及已核验覆盖文件。脱敏后的可移植启动包装器和重建 Dockerfile 经过离线一致性检查，尚未用新构建镜像重新完成线上性能验收。使用重建镜像时，应完整执行本手册的验收步骤。

## 1. 设备、账号与文件准备表

| 对象 | 数量或条件 | 检查方式 |
|---|---|---|
| DGX Spark / GB10 | 4 台，相同架构和兼容驱动环境 | `uname -m`、`nvidia-smi` |
| 交换机连接 | 本次基线为每台两条 100G，双 rail | NIC、交换机端口和 RDMA 测速共同核验 |
| RoCE | v2、对应 IPv4 GID index 3 | sysfs GID 表 |
| 主机 MTU | 两条使用中的接口均为 9000 | `ip link`、带 DF 的 ping |
| QoS | DSCP trust，DSCP24→priority3，PFC3 | `mlnx_qos`、交换机 QoS 状态 |
| Docker | 可启动带 GPU、host 网络、RDMA 的容器 | 小容器 `nvidia-smi` 和设备检查 |
| 权重 | 四节点均有完整、同版本的 UL checkpoint | 文件清单、配置和权重校验 |
| Engram | 每个 rank 的两份分片位于本地 NVMe | 打包 manifest、文件头、大小 |
| SSH | rank0 到 rank1/2/3 无交互访问 | BatchMode 测试 |
| UMA | 模型停机时 `MemAvailable` 至少 100000000 KiB | 启动 preflight 检查 |
| 磁盘 | 权重、Engram、镜像、构建缓存分别预留空间 | `df -h`、`du -sh` |

本次 Docker inspect 报告镜像 Size 约 14.45GB；该后端的统计值不等于解压后 RootFS 或 UMA 使用量。`docker load` 还需要解压后的镜像层空间；权重、Engram 和 Docker 数据目录另占空间，不能只按压缩包大小预留。

部署账号需要读取模型文件、写入工作目录，并具备启动本方案容器的权限。`node.py` 会检查 NIC/PFC 并启动 privileged 容器；若普通账号无法执行相关检查，可使用第 10 节的显式 `sudo` 选项。

## 2. 拓扑与 rank 规划

下表所有 IP 均为文档专用地址，只用于展示结构。部署时替换为实际地址。

| 节点 | rank | 管理 SSH | rail0 IPv4 | rail1 IPv4 | 容器 |
|---|---:|---|---|---|---|
| node0 | 0 | operator@192.0.2.10 | 198.51.100.10 | 203.0.113.10 | luz-switch-head |
| node1 | 1 | operator@192.0.2.11 | 198.51.100.11 | 203.0.113.11 | luz-switch-rank1 |
| node2 | 2 | operator@192.0.2.12 | 198.51.100.12 | 203.0.113.12 | luz-switch-rank2 |
| node3 | 3 | operator@192.0.2.13 | 198.51.100.13 | 203.0.113.13 | luz-switch-rank3 |

rail0 和 rail1 使用不同子网。rank0 的 rail0 地址用于 `--dist-init-addr <head-rail0>:26060`。管理连接可使用独立网络，减少文件传输与模型通信争用。

DGX Spark 的一个物理 QSFP 端口可对应多个逻辑 Ethernet/RoCE 设备，不能单凭系统接口数量判断插了几条线。先核对 `ibdev2netdev` 与实物端口，再选择跨 PCIe domain 的两条实际 rail。NVIDIA 的映射说明见 [ConnectX-7 Networking](https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html)。

本次配置选择：

| rail | RoCE HCA | Ethernet netdev |
|---|---|---|
| 0 | rocep1s0f0 | enp1s0f0np0 |
| 1 | roceP2p1s0f1 | enP2p1s0f1np1 |

大小写是设备名的一部分。若你的接线不同，修改 `hcas`、`netdevs` 和 `rail_ips`，不要只修改其中一项。

这里的双 rail 已经实测接近 196Gb/s 总吞吐。该数字是本次连接方式的测量结果，不能从“两个逻辑接口均显示 200G”直接相加推出 400G。

## 3. 获取并固定发布源码

在一台管理机或 rank0 上执行：

```bash
git clone https://github.com/unholySora/luz-deepseek-flash-all-in-one-switch.git
cd luz-deepseek-flash-all-in-one-switch
git checkout switch-v2026.10.15-b20
cat switch/VERSION
python3 switch/tools/verify_source.py
```

预期最后输出 `passed: true`、915 个构建输入文件和 6 个已核验覆盖文件。

本版本源码基底为 `fb5798c6b73d543b297bc3ff410003549f5d96da`。该基底的 `deploy/adapter`、`deploy/b12x`、`deploy/overlay/o12c1-prod` 与实测构建上下文相比，仅 RoCE proxy C 文件需要本目录覆盖。不要把另一个版本的整个 `deploy/` 目录混进本发布工作树。

原始覆盖文件按字节校验；一般构建输入另提供 LF 归一化校验，用于兼容 Windows/Linux 换行差异。校验清单位于 `switch/image-context/source-manifest.json` 和 `switch/overrides/manifest.json`。

## 4. 在四台机器上记录系统基线

以下为只读检查：

```bash
uname -a
uname -m
nvidia-smi
docker version
docker info --format '{{.Architecture}} {{.DockerRootDir}}'
python3 --version
ibdev2netdev
rdma link show
ip -br address
df -h
free -h
```

确认架构是 aarch64/arm64，GPU 是 GB10，相应 NVIDIA 容器运行时可用。不要在复测中途升级驱动、内核、NCCL 或镜像；先记录版本，必要升级后作为新环境重新验收。

若在原环境已有后台服务，先保存启动入口和健康检查结果。后续启动器会检查其他模型容器和 GPU compute PID；这能避免两套 TP4 模型同时争用同一组机器。

## 5. 网络现状检查：已配置环境优先执行这一节

### 5.1 逐节点核对两条接口

```bash
ibdev2netdev
ip -d link show enp1s0f0np0
ip -d link show enP2p1s0f1np1
sudo ethtool enp1s0f0np0
sudo ethtool enP2p1s0f1np1
sudo mlnx_qos -i enp1s0f0np0
sudo mlnx_qos -i enP2p1s0f1np1
```

需要同时看到链路 UP、符合接线方案的协商速率、MTU9000、DSCP trust 和 priority3 PFC。HCA/网口不同则替换命令中的名字。

### 5.2 GID index 3 的实际类型与 IPv4

```bash
for hca in rocep1s0f0 roceP2p1s0f1; do
  echo "$hca"
  cat /sys/class/infiniband/$hca/ports/1/state
  cat /sys/class/infiniband/$hca/ports/1/gid_attrs/types/3
  cat /sys/class/infiniband/$hca/ports/1/gids/3
done
```

预期状态 ACTIVE、类型 `RoCE v2`。IPv4-mapped GID 的最后 32 位应对应配置中的 rail IP；不能只看到 `RoCE v2` 就忽略地址是否一致。启动器也会执行此项核验。

### 5.3 路由与大 MTU 端到端检查

从 node0 检查到 node1，使用实际地址：

```bash
ip route get 198.51.100.11
ip route get 203.0.113.11
ping -I enp1s0f0np0 -c 4 198.51.100.11
ping -I enP2p1s0f1np1 -c 4 203.0.113.11
ping -I enp1s0f0np0 -M do -s 8972 -c 4 198.51.100.11
ping -I enP2p1s0f1np1 -M do -s 8972 -c 4 203.0.113.11
```

8972 是 IPv4/ICMP 下用于检查 MTU9000 的 payload 示例。逐一检查其余 peer 和两条 rail。小包能通、大包失败时，先排查中间交换机 MTU、端口与 VLAN，不要立即调模型参数。

### 5.4 端口、进程与防火墙

管理 SSH 使用部署环境自己的 SSH 端口，常见为22；API默认8006，TP初始化默认26060。NCCL/Gloo及自研RoCE bootstrap还可能使用进程选择的动态控制端口，因此仅放行8006和26060不一定足够。按所在计算网络的策略允许四个已知节点之间的必要控制通信，并用`ss -lntp`和启动日志确认实际绑定地址。不要让管理网默认路由替代rail之间的直连路由；也不需要为部署模型关闭整机防火墙。

### 5.5 交换机检查

对本次 SN2100/Onyx 环境，可先读取：

```text
show version
show interfaces ethernet status
show vlan
show lldp remote
show roce
show dcb priority-flow-control
```

逐个物理端口核对接入节点、端口速率、VLAN/L2 可达性、MTU、PFC/ECN 和 DSCP 映射。不同 Onyx/Cumulus 版本的配置语法不同，本发布不提供会覆盖现有交换机配置的批处理。已有 RoCE 可工作的交换机无需因部署模型而重配。

NVIDIA 的[交换机组网 playbook](https://build.nvidia.com/playbooks/multi-sparks-through-switch/multi-sparks)说明了端口可达、接口子网和管理网络分离等通用前提；其示例接线方式与本次双线方案不同，不要整份脚本直接覆盖已验证的网络。

## 6. 空白环境的主机网络配置示例

仅在第 5 节确认尚无正确配置时使用。保留管理网络连接，先备份已有 netplan 文件，并核对系统使用的 renderer。下面只增加两条 RoCE 接口配置，不更改默认路由。

node0 示例，实际环境必须替换文档地址：

```yaml
network:
  version: 2
  ethernets:
    enp1s0f0np0:
      dhcp4: false
      mtu: 9000
      addresses: [198.51.100.10/24]
    enP2p1s0f1np1:
      dhcp4: false
      mtu: 9000
      addresses: [203.0.113.10/24]
```

node1/2/3 分别将末位改为 11/12/13。保持两个接口不同子网，各节点在对应 rail 上相互可达。

将审核后的内容保存为 `/etc/netplan/60-luz-switch.yaml` 后执行：

```bash
sudo chmod 600 /etc/netplan/60-luz-switch.yaml
sudo netplan generate
sudo netplan try --timeout 120
```

确认管理 SSH 和两条 rail 都正常后再接受配置。如果现有文件已经定义这些接口，应在原配置体系中调整，避免两个文件给同一接口叠加不同地址。

本次通信 traffic class 为 96，对应 DSCP24，映射到 priority3。安装匹配的 NVIDIA QoS 工具后，可逐接口设置：

```bash
for iface in enp1s0f0np0 enP2p1s0f1np1; do
  sudo mlnx_qos -i "$iface" --dscp2prio=set,24,3
  sudo mlnx_qos -i "$iface" --trust=dscp
  sudo mlnx_qos -i "$iface" --pfc=0,0,0,1,0,0,0,0
  sudo mlnx_qos -i "$iface"
done
```

这些设置来自本次实测环境。工具参数和 QoS 映射机制可参阅 [NVIDIA QoS 文档](https://docs.nvidia.com/networking/display/mlnxofedv571020/quality%2Bof%2Bservice%2B%28qos%29)。交换机侧必须有相应映射，不能只改主机。

若采用 systemd 持久化，把上述设置放入本机 root 管理的脚本，设置 oneshot 服务在网络设备就绪后执行；重启后重新读取 `mlnx_qos`。不要同时保留两个会反复覆盖 QoS 的旧服务。

## 7. 停止模型后验证实际双 rail 带宽

正式测速前先验证链路，并将带宽工具与模型测速分时运行。本节示例假设安装了支持本环境的 `perftest`；先查看本机 `ib_write_bw --help` 确认参数。

node1 开两个服务端进程，分别绑定 HCA 和 TCP 控制端口：

```bash
ib_write_bw -d rocep1s0f0 -i 1 -x 3 -p 18515 --report_gbits -D 20
```

另一个终端：

```bash
ib_write_bw -d roceP2p1s0f1 -i 1 -x 3 -p 18516 --report_gbits -D 20
```

node0 同时启动对应客户端，替换 peer IP：

```bash
ib_write_bw -d rocep1s0f0 -i 1 -x 3 -p 18515 --report_gbits -D 20 198.51.100.11 > /tmp/luz-rail0-bw.txt &
pid0=$!
ib_write_bw -d roceP2p1s0f1 -i 1 -x 3 -p 18516 --report_gbits -D 20 203.0.113.11 > /tmp/luz-rail1-bw.txt &
pid1=$!
wait "$pid0"
wait "$pid1"
cat /tmp/luz-rail0-bw.txt
cat /tmp/luz-rail1-bw.txt
```

单独测通两条 rail 后，必须检查同时运行时的总值；分时单 rail 各 100G 不等于同时总计 200G。记录方向、message size、工具版本、是否双向和是否使用 GPU buffer。本次约 196Gb/s 的链路结果不能混作 GPU 集合通信性能。

如果需要 GPU RDMA 和 NCCL 检查，使用已验证的 CUDA/perftest 或 nccl-tests 构建；记录构建版本与实际参数。NVIDIA 的[双节点性能测量说明](https://github.com/NVIDIA/dgx-spark-playbooks/blob/main/nvidia/connect-two-sparks/assets/performance_benchmarking_guide.md)可作操作参考。

## 8. 准备固定镜像

### 8.1 优先使用 LuZ 基础包 + 公开增量

实测镜像完整继承 LuZ `0.3.1-r3` 的 71 个层，新增压缩层约 5.4MB。现在提供 [约 5.5MB 的脱敏增量](image-delta/README.zh-CN.md)，不需传送整个镜像：

```bash
(cd switch/image-delta && sha256sum -c SHA256SUMS)
python3 switch/tools/assemble_image.py \
  --base /srv/images/dsv41-sglang-optimized-0.3.1-r3.tar \
  --output /srv/images/luz-switch-public-20261015.tar
docker load -i /srv/images/luz-switch-public-20261015.tar
docker image inspect --format '{{.Id}} {{.Architecture}}' luz-switch:20261015
```

本次 containerd image store 下得到：

```text
sha256:d2d66bb82dc9b09780b9d791ae261b8e204239d7c077178a39af8aa6ab7f329c arm64
```

其他 Docker 后端可能返回 config digest，须进一步比较完整 RootFS 列表；具体见增量手册。公开版移除内网 provenance、替换一处模块说明文字、清理最终文件系统的预生成 SSH host keys，因此不再是历史实测 `3aca…` 的同一 index。计算代码未改，真实 Docker load/CPU 容器检查通过，但尚未用公开版重跑 GPU 矩阵。示例 cluster 已采用公开版 ID；使用本机实测 `.Id` 更新它。

四节点只比较 tag 名称不够。完整公开包也可单独取得，文件、SHA 和加载方法见 [images](images/README.md)。镜像不包含运行容器的模型、Engram、个人 SSH、缓存和状态 bind mounts；五个 B20 代码覆盖仍须按后文挂载。清理层不能擦除旧基础层的历史密钥字节，不能把完整归档称为基础层已彻底清洗的镜像。

### 8.2 从基础镜像重建

基础镜像为 `dsv41-sglang-optimized:0.3.1-r3`，本次记录 ID：

```text
sha256:f8095fa8d6ed867a9b40c5a90dbadd942bf12d80443d0b8db76ae24a36e415c1
```

使用本机已有或已获授权来源提供的基础镜像，将它标记为 `luz-base:0.3.1-r3`。不要把私有 registry 地址写入发布文件。

在仓库根目录执行：

```bash
python3 switch/tools/verify_source.py
docker build --build-arg BASE_IMAGE=luz-base:0.3.1-r3 \
  -f switch/Dockerfile -t luz-switch:20261015 .
docker image inspect --format '{{.Id}}' luz-switch:20261015
```

Dockerfile 安装 28 个匹配的 SGLang overlay，并检查修改后的 RoCE proxy C 能编译。它依赖基础镜像已有的完整软件栈，不会从任意 CUDA 镜像自动安装出等价环境。

重建后 image ID 通常不同：把实际 ID 写入私有 cluster 配置，并用相同构建产物分发到四节点。不能把新镜像的 tag 改成旧名字后，继续声称它是实测的 `3aca…` 镜像。

### 8.3 最小环境检查

```bash
docker run --rm --gpus all luz-switch:20261015 nvidia-smi
docker run --rm luz-switch:20261015 python3 -c 'import torch; print(torch.__version__); print(torch.version.cuda)'
docker run --rm luz-switch:20261015 python3 -c 'import sglang; import b12x; print("imports ok")'
```

读取或构建依赖可能写入缓存，因此这些检查不与正式性能压测同时运行。

## 9. 准备模型和本地目录

每台机器使用各自的本地 NVMe 模型目录，示例为 `/srv/models/DeepSeek-V4.1-Flash-UL`。目录名本身不是模型版本证明，应保留实际 checkpoint 的版本、来源与文件校验清单。

至少核对：

- `config.json` 与本版本支持的 V4.1 Flash UL 架构、量化格式匹配。
- tokenizer 和聊天模板完整。
- `model.safetensors.index.json` 存在，所有 weight_map 引用的分片可读。
- 主模型、DSpark draft/confidence head、Engram 权重与 scales 来自同一套 checkpoint。
- 四节点配置、tokenizer 和权重文件一致，没有混入旧 checkpoint 的分片。

可在每节点生成同一顺序的本地校验清单：

```bash
cd /srv/models/DeepSeek-V4.1-Flash-UL
find . -maxdepth 1 -type f -print0 | sort -z | xargs -0 sha256sum > /tmp/luz-checkpoint-SHA256SUMS
```

将四份清单比较。读取全部权重会占用 NVMe 带宽，安排在停机准备阶段。

工作目录示例：

```bash
sudo install -d -o operator -g operator /opt/luz-switch
mkdir -p /opt/luz-switch/engram
mkdir -p /opt/luz-switch/state
mkdir -p /opt/luz-switch/cache
mkdir -p /opt/luz-switch/logs
mkdir -p /opt/luz-switch/ssh
```

替换 owner/group，不要沿用不属于本机的账号。`engram` 为只读模型数据挂载，`state` 和 `cache` 为运行期可写目录，`overrides` 为固定代码覆盖。保持它们的用途独立，方便升级和回退。

## 10. 生成四节点 runtime

在源码工作树中：

```bash
cp switch/cluster.example.json switch/cluster.private.json
```

编辑字段：

| 字段 | 含义 | 修改要求 |
|---|---|---|
| `image` | 本地 Docker tag | 四台机器都有该 tag |
| `image_id` | 不可变镜像身份 | 四台实际 ID 一致 |
| `port` | API 端口 | 默认 8006；不得与其他服务冲突 |
| `nodes[].rank` | TP rank | 严格为 0/1/2/3，各一次 |
| `name` | 生成目录名 | 如 node0，保持唯一 |
| `ssh` | rank0 到该节点的 SSH 目标 | 账号和实际 IP/hostname |
| `root` | 该节点的部署目录 | 绝对路径 |
| `model` | 该节点的模型目录 | 绝对路径 |
| `hcas` | 两条 RoCE 设备名 | 顺序对应 rail0/rail1 |
| `netdevs` | 两条 Ethernet 接口 | 与 HCA 映射对应 |
| `rail_ips` | 两条接口 IPv4 | 对应 GID3 的地址 |
| `sudo` | 可选，默认 false | true 时控制器使用 `sudo -n python3 …/node.py` |

私有配置已经被 `.gitignore` 排除。不要把真实 SSH private key、API key 或密码添加为 JSON 字段。

生成 B20：

```bash
python3 switch/tools/configure.py --cluster switch/cluster.private.json \
  --profile b20-throughput --output switch/generated/site-b20
cat switch/generated/site-b20/manifest.json
```

生成器要求输出目录此前不存在，避免默默覆盖正在运行的配置。目录结构：

```text
site-b20/
  manifest.json
  node0/
    runtime.json
    cluster.json
    node.py
    control.py
    overrides/
  node1/ ...
  node2/ ...
  node3/ ...
```

以下值必须保持：

```text
--context-length 600000
--max-running-requests 16
--max-total-tokens 9600000
--tp-size 4
--ep-size 1
--nnodes 4
--chunked-prefill-size 4096
--prefill-decode-interval 1
--speculative-algorithm DSPARK
```

每节点的 `--node-rank`、`NODE_RANK`、Engram rank 必须一致。生成器替换拓扑、镜像与路径，不调整上述算法参数。

## 11. 分发 runtime 和代码覆盖

分别将本节点的子目录内容分发到其 `root`。node0 示例：

```bash
rsync -a switch/generated/site-b20/node0/ operator@192.0.2.10:/opt/luz-switch/
rsync -a switch/generated/site-b20/node1/ operator@192.0.2.11:/opt/luz-switch/
rsync -a switch/generated/site-b20/node2/ operator@192.0.2.12:/opt/luz-switch/
rsync -a switch/generated/site-b20/node3/ operator@192.0.2.13:/opt/luz-switch/
```

这里不使用 `--delete`，不会把既有 Engram、state 或缓存目录当作多余内容删除。若 `root` 不同，逐个替换目的地址。

runtime 的五个 B20 只读覆盖：

| 容器目标 | 作用 |
|---|---|
| `/opt/dsv41/adapter/sitecustomize.py` | 原生 indexer 接入及启动兼容 |
| `/opt/dsv41/adapter/l2_prefetch.py` | L2 预取控制 |
| `/opt/dsv41/adapter/wo_a_w8.py` | WO-A 权重和预取窗口 |
| SGLang 的 `deepseek_v4_memory_pool.py` | V4 extra-KV page64 |
| `/opt/dsv41/adapter/roce_ar.py` | switched/flat RoCE all-reduce/gather |

覆盖源保留实测字节，详见 `overrides/manifest.json`。不要仅凭同名文件判断已安装正确版本；应核对 SHA256。

## 12. 按 rank 打包 Engram

在每台机器执行，但最后一个参数分别为 0/1/2/3。以下是 rank0：

```bash
docker run --rm \
  -v /srv/models/DeepSeek-V4.1-Flash-UL:/models:ro \
  -v /opt/luz-switch/engram:/engram \
  luz-switch:20261015 \
  python3 /opt/dsv41/site/pack_engram.py /models /engram 0
```

预期每 rank 生成：

```text
engram-l1-r0of4.bin
engram-l14-r0of4.bin
manifest.json
```

其余节点文件名中的 rank 对应 1、2、3。packer 检查索引、行数、权重/scales 所在分片、文件头和大小，并对新生成文件采样核验权重与 scale 字节。

若 checkpoint 已改变，不应仅因现有 Engram 文件头和大小相同就直接复用。先将旧 Engram 路径保留用于回退，使用新目录重新打包并更新 runtime 的对应挂载。

完成判据：两层都存在、manifest 的 rank 与本节点一致、文件可读、工作目录所在磁盘剩余空间正常。

## 13. 配置 rank0 的 SSH 和启动权限

在 node0 的部署账号下准备专用密钥。已有专用密钥时复用本地授权方案；不要覆盖全局 `~/.ssh/id_ed25519`。

```bash
cd /opt/luz-switch
chmod 700 ssh
ssh-keygen -t ed25519 -f ssh/id_ed25519 -N '' -C luz-switch-control
chmod 600 ssh/id_ed25519
```

只把 `.pub` 分发到 node1/2/3 对应账号的 `authorized_keys`。通过可信控制台或已有可信连接核对各节点 SSH 主机指纹，写入 `ssh/known_hosts`。本方案开启 `StrictHostKeyChecking=yes`；不要用关闭检查来绕过主机身份错误。

测试与控制器相同的 SSH 选项：

```bash
ssh -i /opt/luz-switch/ssh/id_ed25519 \
  -o UserKnownHostsFile=/opt/luz-switch/ssh/known_hosts \
  -o StrictHostKeyChecking=yes -o BatchMode=yes \
  operator@192.0.2.11 python3 /opt/luz-switch/node.py status
```

对其余两个 worker 同样测试。若私有配置设置 `sudo:true`，还需测试远端 `sudo -n python3 /opt/luz-switch/node.py status`。部署环境应事先提供对应非交互权限，控制器不会传送或保存 sudo 密码。

rank0 读取密钥的账号应与运行 `control.py` 的账号相符。若用 root 执行控制器，root 必须能读取部署目录内这套专用密钥和 known_hosts。

## 14. 停止已有模型并逐节点 preflight

先使用旧服务自己的控制入口停止旧模型，保存它的配置和恢复命令。随后在四台机器检查：

```bash
docker ps --format '{{.Names}} {{.Status}}'
nvidia-smi --query-compute-apps=pid --format=csv,noheader
grep -E 'MemAvailable|SwapTotal|SwapFree' /proc/meminfo
ss -lnt
```

没有其他 GPU 模型进程，API 端口和 TP 初始化端口未被其他服务占用。不要手动 kill 不认识的进程来绕过 preflight。

在各节点的 root 目录执行：

```bash
cd /opt/luz-switch
sudo python3 node.py start --dry-run
```

它检查 image tag 对应的 image ID、其他模型容器/GPU PID、HCA ACTIVE、RoCE v2 GID3 与地址、MTU9000、DSCP trust、PFC3、Engram 分片以及足够的空闲 UMA。

若普通部署账号不需要 sudo，则用普通账号重跑一次，确保远端控制器会采用的权限路径也能成功。

## 15. 启动集群并确认真实容量

在 node0：

```bash
cd /opt/luz-switch
python3 control.py start
```

worker 先启动，head 后启动。控制器等待健康状态，并检查 pool、context、运行请求上限和模型名。启动过程可能包含模型加载、缓存构建、graph capture；首次启动比已有缓存的重启慢，不要用首个 API 端口打开的时刻判定完全就绪。

查询：

```bash
python3 control.py status
curl -fsS http://127.0.0.1:8006/health
curl -fsS http://127.0.0.1:8006/v1/models
curl -fsS http://127.0.0.1:8006/server_info | python3 -c 'import json,sys; d=json.load(sys.stdin); print({k:d[k] for k in ["context_length","max_running_requests","max_total_num_tokens"]})'
```

预期：

```json
{"context_length": 600000, "max_running_requests": 16, "max_total_num_tokens": 9600000}
```

日志：

```bash
python3 node.py logs 150
docker logs --tail 150 luz-switch-head
```

worker 也应检查启动日志。记录容器 ID、StartedAt、image ID 和 runtime SHA，后续性能结果必须能对应到具体实例。

```bash
sha256sum runtime.json
docker inspect --format '{{.Id}} {{.Image}} {{.State.StartedAt}} {{.State.OOMKilled}}' luz-switch-head
```

## 16. 最小业务与功能验收

先发一个无 thinking 的短请求：

```bash
curl -fsS http://127.0.0.1:8006/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4.1-flash-ul-luz","messages":[{"role":"user","content":"计算17+25，只输出整数。"}],"temperature":0,"max_tokens":32,"chat_template_kwargs":{"thinking":false}}'
```

在可访问该 API 的机器，从源码根目录运行功能 client：

```bash
python3 switch/qualification/functional_accept.py \
  --base http://127.0.0.1:8006 --model deepseek-v4.1-flash-ul-luz \
  --output /tmp/luz-functional-001 --context-length 600000 \
  --expect-running-limit 16 --min-token-pool 9600000 --concurrency 8
```

它覆盖算术、thinking、SSE、工具调用、JSON schema、图片、中等上下文、C8 正确性及持续 C8。检查每项 passed，而不是只看 HTTP200。

近 600k 召回检查单独运行：

```bash
python3 switch/qualification/needle_600k.py --output /tmp/luz-needle-001
```

该 client 会先用服务器 tokenizer 调整输入，放置三处记录，再分别测 cold 与 prefix-hit。cold 与 prefix-hit 时间分开保存，不能取缓存命中时间冒充长文首次 prefill。

这仍是单路长文检查。后续独立的[KV 驻留/召回实测](reports/KV-RECALL.md)记录逐请求余量、同时占用、各位置召回和资源结果；复现该测试需使用[独立监测与会话修正指导](qualification/KV-MONITOR-GUIDE.zh-CN.md)，不能只启用本段单路脚本就扩大资格。

## 17. Decode 复测：旧基线和 structured v2

测试期间停止其他 API 压测、镜像压缩、权重校验、文件大拷贝和 profiler。若服务接真实业务，应安排独立测试窗口。

SD-1 的核心设置是 streaming chat、temperature0、top_p1、thinking off、强制完整输出。每流 decode 为 `(completion_tokens−1)/(t_last−t_first)`；聚合为总 decode token 除以整个波次的 union 窗口。聚合不能用 C×单流中位替代。

旧四场景回归：

```bash
python3 switch/bench/bench.py --base http://127.0.0.1:8006 \
  --model deepseek-v4.1-flash-ul-luz --output /tmp/luz-decode-v1-001 \
  --run-tag site-v1-001 --types prose,code,json,structured --concs 1,2,4,8 \
  --waves 3 --max-tokens 2048 --paired-suite switch/bench/matrix-prompts.json \
  --expect-context 600000 --expect-running 16 --expect-pool 9600000
```

更新后的 structured v2：

```bash
python3 switch/bench/bench_structured_v2.py --base http://127.0.0.1:8006 \
  --model deepseek-v4.1-flash-ul-luz --output /tmp/luz-structured-v2-001 \
  --run-tag site-structured-v2-001 --types structured --concs 1,2,4,8 \
  --waves 3 --max-tokens 2048 --prompt-tag round2-structured-v2-20261015 \
  --expect-context 600000 --expect-running 16 --expect-pool 9600000
```

v2 自动令 `count_max=max_tokens`，所以这里要求从 1 数到 2048。换成 4096-token 输出时，只需改 `--max-tokens 4096`，数字任务上限同步变成 4096。它不接受旧 `--paired-suite`，防止 C1 被旧提示覆盖；实际生成的完整 suite 保存在输出目录。

跨服务比较时使用相同 max_tokens、waves 和 prompt-tag，逐条确认实际提示 SHA 相同。不同测试版本不能混入同一个均值；新 structured 行可替换展示表中的 structured，但必须标注“v2新测”，其他行保留原始 run 和时间。

检查四类数据：

1. 每流 completion_tokens 是否达到预算，是否收到 `[DONE]`，是否请求错误。
2. 数字输出是否仅包含数字/分隔符、从 1 连续递增、没有重启计数。预算在数字中间耗尽时，最后一个数字可为预期数字的合法前缀，client 会单独记录。
3. 每波的 first-token spread、结束时间差、单流分布与聚合。
4. `metrics_before/after` 的请求级 verify 总和、generation 总和，以及资源记录。

`spec_verify_calls_total` 是已完成请求的 verify 次数之和。在 C>1 时，不能把 wall time 除以这个 counter 当作 GPU 单步延迟。GPU step 成本需要独立 trace。

## 18. Prefill 复测

公开 repeated-filler 口径：

```bash
python3 switch/bench/sd_bench.py --mode pr \
  --sizes 8192 32768 65536 131072 --pr-filler repeated \
  --base-url http://127.0.0.1:8006/v1 --key-file /nonexistent \
  --label site-pr-first --json-out /tmp/luz-pr-first.json
```

按报告口径，还需 first、warm、arb1/2/3 分开保存，仲裁轮次轮换输入档位顺序，取后三次中位。数值分子用服务实际返回的 prompt_tokens，分母为 TTFT。短 prompt 的 SD-1 prefill 栏、长文 repeated-filler PR、prefix-hit 重放不是同一个测量。

冻结模板散文口径：

```bash
python3 switch/bench/paired_real_prefill.py \
  --base http://127.0.0.1:8006/v1 --model luz \
  --output /tmp/luz-pr-generated-001 \
  --prompts switch/bench/real-prefill-prompts.json \
  --instrument switch/bench/sd_bench.py
```

这里的“real”来自上游语法模板生成器，近似自然文本分布，不是外部业务文档数据集。32k/128k 是字符估算档位；实际输入约 28k/111k token，以响应中的实际值为准。

该 client 使用已经固定的生成器/量具 SHA，不宜随手替换 `sd_bench.py`。比较另一端时，输入与 warmup SHA 必须逐条相同。

## 19. 资源观测与指标解释

每次压测同时记录四节点：`/proc/meminfo`、容器 memory/swap、OOM-kill、GPU 温度/频率、服务 running/queue、KV 使用与 retract。仅看 `nvidia-smi` 的显存栏不足以评估 GB10 UMA。

常用只读命令：

```bash
free -h
grep -E 'MemAvailable|SwapTotal|SwapFree' /proc/meminfo
vmstat 1
docker stats --no-stream luz-switch-head
docker inspect --format '{{.State.OOMKilled}} {{.State.Status}}' luz-switch-head
nvidia-smi --query-gpu=name,pstate,temperature.gpu,clocks.sm,power.draw,utilization.gpu --format=csv
curl -fsS http://127.0.0.1:8006/metrics > /tmp/luz-metrics.txt
```

B20 正式 decode 窗口头节点最低可用约 8.11GiB，长文质量窗口可能更低；它是特定实例下的观测值，不是固定剩余容量保证。容器 swap=0 和无 OOM 不等于整个宿主从未发生 swap；应连同宿主增量解释。

观察热稳定状态与频率。首次冷编译、graph capture、后台镜像压缩及大文件读写会影响测量，遇到差异先查是否处于同一工作状态。

## 20. 可选 B32 省内存配置

保持旧配置可回退，用新生成目录：

```bash
python3 switch/tools/configure.py --cluster switch/cluster.private.json \
  --profile b32-memory --output switch/generated/site-b32
```

先停止 B20，再分发各节点的新 runtime 和 overlays，然后按第 14–18 节重验。B32 额外挂载 `overrides/b32/kv_fp4_repack.py`，启用 FP4 extra-KV 与分档 repack。

实测 B32 相比 B20 短测头节点多留出约 6.6GiB 可用内存，但 decode 有损失；不要把省内存数字直接外推为更高吞吐或已经验证更长上下文。B32 的业务范围是 prose/code 的有限 C1/2/4/8 试验和 C8 功能、单路近 600k 召回，不是完整四场景替代方案。

## 21. 接入已有模型管理平台

平台登记应使用本地部署者自己的连接信息，不把实际管理网地址和授权写入仓库。常用字段如下：

| 字段 | 示例或要求 |
|---|---|
| 显示名 | `LuZ Switch B20` |
| served model | `deepseek-v4.1-flash-ul-luz` |
| OpenAI base URL | `http://<head-management-address>:8006/v1` |
| 健康检查 | `http://<head-management-address>:8006/health` |
| 启动入口 | 在 rank0 执行 `python3 <root>/control.py start` |
| 停止入口 | 在 rank0 执行 `python3 <root>/control.py stop` |
| 状态入口 | `python3 <root>/control.py status` |
| 容量元数据 | context600000、MRR16、KV9600000 |

平台需要的 SSH/API 授权由其现有凭据管理机制保存。若平台会自动反复调用生成型健康检查，应将这些请求对 benchmark 的影响计入控制条件；本次 LuZ 禁用了 health endpoint generation，hybrid_v2 保留自身配置。

切换默认模型前，先确认所需功能和实际端点已验收。首次发布收尾曾恢复 hybrid_v2；同日下午使用者明确选择 luz-switch，因此后续 KV 测试收尾恢复原 B20 配置并保持为默认，hybrid_v2 停止备用。此操作不代表 LuZ 已在 C1 prose/code 两场景稳定全面胜出；两个会话测试修正和 prefill1 候选独立归档，未混入原 B20 默认。部署目录的存在也不代表平台会自动选择它。

## 22. 常见故障与排查顺序

| 症状 | 优先检查 | 处理原则 |
|---|---|---|
| Image identity mismatch | tag 实际 ID 与 cluster/runtime | 使用同一实测镜像或明确记录新构建 ID，重新生成四 rank |
| HCA 不存在或 GID 不符 | 接线、大小写、ibdev2netdev、GID3 IPv4 | 修正拓扑字段，重新生成；不绕过 preflight |
| MTU/PFC 断言失败 | 两端 NIC、交换机 MTU 和 QoS | 先恢复网络一致性，保留旧配置记录 |
| Another GPU model is running | 旧平台服务和计算 PID | 通过旧服务控制入口停止，再重试 |
| SSH BatchMode/host key 失败 | 专用密钥、authorized_keys、known_hosts、账号 | 核验主机指纹及文件权限 |
| sudo 需要密码 | cluster 节点 sudo 字段与非交互授权 | 在本机配置好权限，不把密码写进脚本 |
| Engram 文件缺失/错误 rank | 打包节点 rank、两层文件、模型版本 | 为正确 rank 和 checkpoint 重新生成新目录 |
| TP 初始化卡住 | head rail0、26060端口、两条rail连通性、worker日志 | 先验证四节点网络和 rank 唯一性 |
| 600k/16/9.6M 未体现 | server_info 与实际启动参数 | 不接受只改平台展示字段的结果 |
| decode 明显低于报告 | 提示版本、full length、thinking、cap、镜像/覆盖SHA、温度、后台任务 | 先做同口径 C1 prose/code，再扩并发 |
| structured 聚合低 | 旧v1强制扩写、accept收益、最慢流、活动槽 | 使用v2重测并保留旧结果，查看连续性与逐波数据 |
| prefill 数值异常高 | prefix hit、重复填充、实际prompt_tokens、TTFT | 明确冷态/热态与输入分布，不混合归因 |
| OOM 或强烈 swap | UMA余量、其他任务、缓存、KV布局 | 保存现场，停止本服务，回退已知可用配置 |

日志通常包含主机路径、IP、请求和业务数据；对外报告前重新脱敏，不能直接把整份 docker inspect、环境变量或平台数据库提交到 GitHub。

## 23. 停止、回退和升级

正常停止在 rank0：

```bash
cd /opt/luz-switch
python3 control.py stop
python3 control.py status
nvidia-smi --query-compute-apps=pid --format=csv,noheader
```

若是部分启动失败，控制器会尝试停止本次启动的实例，并核对 expected container ID，避免误停后来替换的实例。排障时保留失败日志和 runtime，不要通过删除全部 Docker 资源清场。

回退到 B20：停止集群；重新分发之前保留的 B20 四 rank runtime 与其 overlays；核对 image ID 与模型版本；重新执行 preflight、启动和容量检查。回退到其他模型则使用那套模型自己保存的控制入口。

升级时创建新生成目录和新发布记录，记录 Git tag、镜像 ID、四 rank runtime SHA、六个覆盖源 SHA、模型文件清单、Engram manifest 和测试 run。这样才能分辨代码变更、环境变更和输入变化。

## 24. 最终交付与验收记录模板

建议为每次实际部署保留一份本地记录：

```text
Git tag / commit:
Docker image ID (four nodes):
Model checkpoint identity and checksum manifest:
Engram rank0/1/2/3 manifests:
runtime SHA256 (four nodes):
RoCE HCA/netdev/GID/MTU/PFC checks:
Dual-rail bandwidth command, direction and results:
server_info context/MRR/pool:
Functional checks:
Single near-600k recall (cold / prefix-hit):
Decode v1 run:
Structured v2 run / output budget / count_max:
Full / short / error / correct-sequence counts:
Prefill repeated / generated-text runs:
Resource minima, swap and OOM evidence:
Default service and rollback entry:
```

公开结果只保留必要数值、输入版本和匿名节点身份；完整运维记录留在自己的受控工作区。对应本次完成的数据、局限和后续方向见 [RESULTS](reports/RESULTS.md)、[STRUCTURED](reports/STRUCTURED.md)、[TUNING](reports/TUNING.md)。
