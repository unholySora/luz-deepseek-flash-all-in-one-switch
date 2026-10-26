# LuZ Switch：2026-10-15 B20

**性能资料更新（2026-10-26）：** 已归档 [10/22 新固件完整性能及旧固件 B20 对照](reports/FIRMWARE-PERFORMANCE-20261022.zh-CN.md)。1017-UL / 400-token 口径覆盖 C1/C2/C4/C8 四场景，共 240 轮、900 个正式请求；prefill 为 8K/64K/256K。C1 prose/code/json/structured 为 90.94/142.45/144.44/169.27 token/s，C8 聚合为 257.50/347.52*/557.65/319.41* token/s。`*` 含短于 400 token 的请求，保留实际长度。与 10/20 完整旧基线的聚合速度比几何平均为 +4.84%，不作为固件因果收益。原始数值、测试时两项 USB-PD 待确认状态及复核工具均在新报告中。

此更新不修改下方部署配置或历史验收；下方 2048-token 重测命令对应 10/15 旧报告，新固件 400-token 口径的命令和冻结量具见新报告第 9 节。

四台 DGX Spark、每台双 100Gb/s RoCE、通过交换机连接的部署版本。默认配置为 **600000 上下文、16 个运行请求、9600000 token KV 池**。完整速度验收覆盖 C1/C2/C4/C8。C1/2/4/8/12/16 的近满池驻留与召回已完成，C16 的实际输入、页图及资源结果见[独立 KV 实测报告](reports/KV-RECALL.md)。该报告的测试配置显式加入两项 streaming-session 运行时修正；后续高并发容量候选另加 prefill 每批 1 请求限制。原 B20 的速度资格范围不扩大。

本目录保留与原项目并列的部署方案。原仓库的部署文件保持原样。实测源码覆盖文件保留原始字节；IP、SSH 用户、模型和工作目录改为参数化模板。首次安装请先阅读 [极详细部署手册](DEPLOYMENT-GUIDE.zh-CN.md)，面向上游的差异说明见 [LuZ技术报告](UPSTREAM-REPORT.zh-CN.md)。实验细节见 [调优说明](reports/TUNING.md)、[实测结果](reports/RESULTS.md) 和 [structured 分析](reports/STRUCTURED.md)。

## 两个配置

| 配置 | 用途 | extra-KV | 验收范围 |
|---|---|---|---|
| `b20-throughput` | 默认速度候选 | V4、page64 | 四场景 C1/2/4/8 各三轮；C8 功能；单路 599440-token 三处召回 |
| `b32-memory` | 可选省内存配置 | FP4 + C8 hash repack | prose/code C1/2/4/8；C8 功能；单路 599440-token 召回；没有完整四场景矩阵 |

B20 相比上一轮 A31，仅调整 `DSV41_VERIFY_CAP=conf:0.15` 和 `--prefill-decode-interval=1`。其余已验收设置包括原生 FP4 indexer、V4 extra-KV page64、独立 WO-A L2 预取、512KiB flat RoCE all-reduce/gather、每层 0.5GiB Engram 缓存、低噪声日志和 C1–C8 的 LM head 配置。B32 在试验中多留出约 6.6GiB 主节点可用内存，但 decode 较慢，因此单独保留。

## 准备镜像

实测镜像相对 LuZ `0.3.1-r3` 完整复用 71 个基础层，只增加约 5.4MB 压缩层。已在 [image-delta](image-delta/README.zh-CN.md) 发布约 **5.5MB 的脱敏增量**、重组工具和逐步说明；完整镜像与权重不上传仓库。用指定的 LuZ 基础 `.tar` 加增量即可重组并 `docker load`，无需重新编译依赖。

实测 index 为 `3aca5abc…`，脱敏公开版 index 为 `d2d66bb8…`（完整 SHA 见 [镜像说明](images/README.md)）。公开版移除构建来源中的内网地址、替换一处模块说明文字并清除最终文件系统的预生成 SSH host keys；计算代码保持原样。真实 Docker load 和 CPU 容器检查已通过，GPU 性能矩阵仍对应原实测镜像。

也可从原项目要求的 `dsv41-sglang-optimized:0.3.1-r3` 基础镜像构建。先将本机已有基础镜像标记为 `luz-base:0.3.1-r3`，然后在**仓库根目录**执行：

```bash
docker build --build-arg BASE_IMAGE=luz-base:0.3.1-r3 \
  -f switch/Dockerfile -t luz-switch:20261015 .
docker image inspect --format '{{.Id}}' luz-switch:20261015
```

构建使用根目录既有源码，加上 `switch/image-context/_roce_proxy.c`。915 个输入文件的清单和 SHA256 位于 [source-manifest.json](image-context/source-manifest.json)。基础镜像必须包含匹配的 CUDA、PyTorch、SGLang、NCCL 和 b12x_next 环境，不能替换成任意 CUDA 镜像。脱敏后的 Dockerfile 与跨机器启动包装器做过离线源码和参数一致性校验，尚未以新构建镜像重新完成线上测速；实测数值对应上面的固定镜像 ID。

## 生成和分发配置

需要四台机器有相同模型文件、足够的 UMA 和 NVMe 空间，以及同一镜像。网络前提为双 RoCE v2、GID index 3、MTU 9000、DSCP trust、PFC priority 3。示例 NIC 名称取自 GB10 平台，按实际设备修改。此目录不自动改动交换机配置。

```bash
cp switch/cluster.example.json switch/cluster.private.json
# 编辑四台机器的 SSH、root、model、HCA、netdev、rail_ips。
# 若自行重建镜像，将 image_id 改为 docker image inspect 返回的实际值。
python3 switch/tools/configure.py --cluster switch/cluster.private.json \
  --profile b20-throughput --output switch/generated/site-b20
```

例子使用文档专用地址，必须替换为实际地址。两条 rail 使用各自子网；`rank=0` 的 rail0 地址用于 TP 初始化。生成器只替换拓扑、主机路径和镜像标识，所有算法参数保持选中配置的值。`b32-memory` 的生成方式相同，输出目录另取一个新名字。

将 `generated/site-b20/nodeN/` 中的 `runtime.json`、`node.py`、`control.py`、`cluster.json` 和 `overrides/` 分发到该节点的 `root` 目录。保留各节点已有的 `engram/`、`state/` 和 `cache/`。在 rank0 的 `root/ssh/` 下自行配置专用 `id_ed25519` 和已核验的 `known_hosts`，允许 rank0 免交互 SSH 到其余节点；SSH 密钥不入库。

首次部署需在每个节点上按本节点 rank 打包 Engram。示例中的镜像、目录和 rank 按实际替换：

```bash
mkdir -p /opt/luz-switch/engram
docker run --rm -v /srv/models/DeepSeek-V4.1-Flash-UL:/models:ro \
  -v /opt/luz-switch/engram:/engram luz-switch:20261015 \
  python3 /opt/dsv41/site/pack_engram.py /models /engram 0
```

## 启动、停止和检查

确认上一套 TP4 模型服务已停止。先在各节点执行 `sudo python3 /opt/luz-switch/node.py start --dry-run` 检查镜像 ID、NIC、PFC、UMA 和 Engram 分片。启动入口在 rank0；它需要有 Docker、RDMA 和资源锁定权限，且远端 SSH 用户应具备相应权限。

```bash
cd /opt/luz-switch
python3 control.py start
python3 control.py status
curl -fsS http://127.0.0.1:8006/server_info
python3 control.py stop
```

实际 `/server_info` 必须报告 `context_length=600000`、`max_running_requests=16`、`max_total_num_tokens=9600000`。启动器保留原实验的容器内存限制、主机网络和 RDMA 权限参数；不要将普通容器 RAM 指标误作 GB10 全部 UMA 占用。

## 重测

decode 使用冻结提示和 SD-1 计时，必须区分完整 2048 输出和短流。所有重测均使用新的输出目录。以下命令从仓库根目录执行，测试期间不要同时打包镜像或运行其他压测：

```bash
python3 switch/bench/bench.py --base http://127.0.0.1:8006 \
  --model deepseek-v4.1-flash-ul-luz --output /tmp/luz-switch-test-001 \
  --run-tag site-test-001 --types prose,code,json --concs 1,2,4,8 \
  --waves 3 --paired-suite switch/bench/matrix-prompts.json \
  --expect-context 600000 --expect-running 16 --expect-pool 9600000
python3 switch/bench/bench_structured_v2.py --base http://127.0.0.1:8006 \
  --model deepseek-v4.1-flash-ul-luz --output /tmp/luz-structured-v2-001 \
  --run-tag structured-v2-001 --types structured --concs 1,2,4,8 \
  --waves 3 --max-tokens 2048 --prompt-tag round2-structured-v2-20261015 \
  --expect-context 600000 --expect-running 16 --expect-pool 9600000
python3 switch/bench/sd_bench.py --mode pr --sizes 8192 32768 65536 131072 \
  --pr-filler repeated --base-url http://127.0.0.1:8006/v1 \
  --key-file /nonexistent --json-out /tmp/luz-switch-pr-001.json
```

`json` 和 `structured` 是提示类别，未启用 grammar。当前 structured v2 随 `--max-tokens` 自动调整计数上限；旧v1四场景回归仍可用原bench运行，数据单独保留。`structured` 原始提示要求数到 200，却强制继续到 2048 token；因此后段可能脱离数字序列，这影响投机接受率和长尾，详见分析。功能性 JSON schema、工具调用和 C8 正确性另行检查。

首次发布时默认保留 hybrid_v2；同日下午使用者明确要求改为 luz-switch，因此后续 KV 测试收尾保持原 B20 运行，hybrid_v2 停止备用。B20 尚未证明在 prose/code 两场景稳定全面胜过它。会话测试修正和 prefill1 容量候选均单独归档，未混入原 B20 默认。

## 满池 KV 召回

[验收方案与操作](qualification/KV-RECALL-PLAN.zh-CN.md) 提供公版《三國志演義》背景、每流独立随机记录、分词精确的输入生成器及 session 召回评分工具。C1/2/4/8/12/16 的近满池驻留与召回已完成，C16 的实际输入、页图及资源结果见[KV 实测报告](reports/KV-RECALL.md)，同时提供[监测与会话修正部署指导](qualification/KV-MONITOR-GUIDE.zh-CN.md)。原入场 C8 的召回正确但资源余量未通过，后续另测 prefill1；报告保留两轮结果及各自范围。
