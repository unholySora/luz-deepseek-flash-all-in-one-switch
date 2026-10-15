# 四 rank KV 实际持有监测与会话测试配置

这是独立验收窗口使用的工具。基础为 B20 的实测镜像和 KV/数值配置，目标总上下文 600000、最大运行 16、实际 FULL 池 9600000。它增加了只读探针、streaming-session 接口，以及两个分别修复满行入场、首次长 prefill 缓存维护的 opt-in 模块。原同批 prefill C8 虽召回正确，头节点可用内存曾降到 2.52GiB，资源余量未签收；后续候选另加 `--prefill-max-requests 1`。不能把该配置写成“完全未修改的 B20 streaming-session 已通过”。正式 B20 启动文件和既有版本标签保留。

语料、评分与容量边界见 [原方案](KV-RECALL-PLAN.zh-CN.md)。本工具不负责启停默认服务，也不接触 API Key 策略。需要运维方按现场四 rank 的实际路径设置挂载与控制；不要把示例中的容器路径当作主机路径。

## 各模块的职责

| 文件 | 行为 | 启用条件 |
|---|---|---|
| `overrides/kv_residency_probe.py` | 包装 scheduler 的输入处理，约每秒记录 CPU 元数据；显式 deep 命令时复制逻辑页映射并抽样已有 backing；记录 match/retract/release | `LUZ_KV_PROBE=1` |
| `overrides/session_row_admission.py` | 全局 request-row 空闲数为零时，允许合法 continuation 复用自己的行，保留新请求和 beam 排队 | `LUZ_STREAMING_ROW_ADMISSION_FIX=1` |
| `overrides/session_chunk_cache.py` | 首次流式 turn 尚无 held slot 时，分块 prefill 委托原生 cache 维护，避免跳过 SWA 回收 | `LUZ_STREAMING_FIRST_CHUNK_CACHE_FIX=1` |
| `overrides/memory_watch.py` | 按指定间隔读主机/容器内存、swap 增量、OOM 与 memory pressure，写 fatal 标记；原轮 2 秒、prefill1 候选 0.5 秒 | 独立主机进程，传入容器名和全新输出目录 |
| `barrier_recall_worker.py` | native API 填充、保留、并发查询和关闭；在四个阶段等待独立监测方放行 | 600k/9.6M/C16 专用，当前使用 head 本地 `127.0.0.1:8006` |

只读探针不申请/释放 KV、不改请求内容或采样、不增加分布式 collectives。deep 的 GPU→CPU 复制、hash 和文件输出存在诊断开销，因此这不是用于正式速度排名的 profiler。admission 修正具有调度行为变化，不能把它包含在“只读探针”的描述内。

## 先做 CPU 检查

从**实际运行容器（含声明的 bind mounts）**取得 `sglang/srt/mem_cache/memory_pool.py`，作为 row allocator 测试输入，不要用另一版上游文件或未叠加覆盖文件的镜像层代替。测试会提取其中 `ReqToTokenPool.alloc/alloc_rows` 的真实实现。

```bash
python3 switch/qualification/test_recall_transport.py
python3 switch/qualification/overrides/test_session_row_admission.py \
  --memory-pool-source /path/to/extracted/memory_pool.py \
  --receipt /path/to/new/row-admission-cpu.json
python3 switch/qualification/overrides/test_session_chunk_cache.py \
  --streaming-session-source /path/to/extracted/streaming_session.py \
  --receipt /path/to/new/first-chunk-cpu.json
```

row-admission 检查覆盖原版满行阻塞、16 行复用、普通请求排队、无效/beam 请求、正在运行的行、重复行、batch 上限、异常恢复及有空位时的原路径。它不能替代 GPU 推理。

首次 chunk-cache 修正另有 7 项分支合约检查。此前一次 128k 首轮 prefill 在 SWA 已用 125184/125440、FULL 远未满时停滞；streaming shortcut 跳过了普通 cache 的窗口维护。修正仅让“首次 turn 尚无 held slot”的 chunk 委托原生 cache，保留之后的 session finish/restore。已有 slot 的大幅长追加仍沿用原实现，未据此宣称通过任意长度的多轮追加。

探针 CPU 检查可在装有 torch 的相同镜像中运行：把 `switch/qualification/overrides` 只读挂到 `/probe`，以 Python 为 entrypoint、`NODE_RANK=0`，运行 `/probe/probe_cpu_test.py`。不给 GPU，并限制容器内存。它验证正确页图、跨流页别名、held/free 冲突的检测，以及输入映射未被修改；不是 GPU 数值测试。

## 制作四 rank 的独立测试配置

1. 逐 rank 备份已验收的 runtime JSON 和其 SHA256。保持原镜像 ID、权重、网络、KV 布局、cap、gather、上下文、MRR、池和 graph 参数。
2. 复制 B20 的 `sitecustomize.py` 到新的测试目录。在其 scheduler 导入分支、唯一的 `install_idle_release(module)` 后加入下列代码。不要修改原 B20 文件。先检查插入点只出现一次并用 `compile()` 检查语法。

   ```python
   from session_row_admission import install as install_session_row_admission
   install_session_row_admission(module)
   from session_chunk_cache import install as install_session_chunk_cache
   install_session_chunk_cache(module)
   from kv_residency_probe import install as install_kv_probe
   install_kv_probe(module)
   ```

3. 在所有 rank 将测试副本只读挂到原 `/opt/dsv41/adapter/sitecustomize.py`，并将三个模块分别只读挂到相同 adapter 目录，名称保持一致。
4. 所有 rank 增加环境变量：

   ```text
   LUZ_KV_PROBE=1
   LUZ_KV_PROBE_DIR=/state/kv-recall-<run>/probe
   LUZ_STREAMING_ROW_ADMISSION_FIX=1
   LUZ_STREAMING_FIRST_CHUNK_CACHE_FIX=1
   ```

5. 在真正传给 `sglang.launch_server` 的参数数组中追加 `--enable-streaming-session`。复现 prefill1 候选时再加 `--prefill-max-requests 1`；本站 JSON launcher 使用 `args`，仅添加名为 `EXTRA_SGLANG_ARGS` 的环境变量不会生效。
6. 安全停止原服务后启用测试配置。确认四 rank 的镜像、配置 hash 和挂载一致。head `/server_info` 必须返回上述 600000/9600000/16，且 `enable_streaming_session=true`；prefill1 候选还必须返回 `prefill_max_requests=1`。

prefill1 限制每批新入场的 prefill 请求数，不把最大运行 decode 数改成 1。客户端仍同时提交 C 路查询。探针另记录查询阶段的 `peak_running_req_count`，小规模 C16 实测已经出现 16 个 running 请求；不能仅依据启动参数推断真正的活动 batch。降低 prefill 峰值可能增加排队或尾延迟，需同时保留查询延迟，本次没有因此追加普通业务吞吐资格。

不能通过把 MRR 改为 17、增大 request-row pool 或释放一个流来绕过满行问题，再声称完成原 C16 驻留测试。修正模块保留 16 行，并最终由原 allocator 复用已持有的行。

## 每一级的四个监测屏障

worker 逐流冷填充并保留所有 session；全部填满后才同时提交 C 路查询。这样验证 C 份独立长历史同时持有及后续复用，不等价于 C 路长输入同时提交的入场压力或 prefill 吞吐测试。

使用新 seed/输入目录和全新输出目录，在 head 启动：

```bash
python3 -u switch/qualification/barrier_recall_worker.py \
  --suite /path/to/prepared-c1 \
  --output /path/to/new/c1-result \
  --stage c1-unique-run \
  --prefill-limit 1
```

worker 的 `status.json` 依次出现：

| 屏障 | 监测方必须检查 | 允许继续的文件 |
|---|---|---|
| `before` | 四 rank 无上一级遗留 session slot，资源监测健康 | `release-before` |
| `plateau` | 本级所有流均已 ACK；四 rank 全部有效持有、长度符合输入，独立页无冲突、不在 free/release 列表，历史 backing 有非零抽样 | `release-plateau` |
| `after-query` | 全部查询结束；KV 仍持有，历史同位置抽样稳定，查询复用长度符合预期，无 retract；评分已保留原响应 | `release-after-query` |
| `closed` | 本次所有 slot 已释放，记录 allocator/cache 回收状态和内存 | `release-closed` |

监测失败就不要创建放行文件。worker 等待单个屏障最多 180 秒，异常会请求关闭自己的 session。主控仍应有外部恢复措施，进程突然退出不保证能执行 Python `finally`。

### 触发四 rank deep 快照

从 worker `status.json` 取得本级 `sessions` 和 `positions`，向**每台主机**对应的 `/state` 挂载目录写入同一个命令，例如：

```json
{"id":"c1-plateau-unique","deep":true,"reset_peak_after":true,"positions":{"<actual-session-id>":[6000,60000,150000,300000,450000,540000,590000]}}
```

上面的位置仅示意，正式使用准备集的真实 `record_token_positions`，不能照抄。`reset_peak_after=true` 仅用于 plateau：保存快照后重置 CUDA allocator 峰值统计和 running 计数器，以便 after-query 记录查询窗口的峰值；不改张量、KV 或采样。其他屏障不加此选项。保留 `peak_reset.command` 并核对它等于本级 plateau 命令，避免把启动或上一阶段峰值当成本级查询峰值。

以临时文件 + 原子 rename 写 `probe/request.json`，避免读到半个 JSON。等待每个 rank 各自的 `<id>-rankN.json` 和对应 `*-maps.npz`；收集四份后再放行。错误写在 `error-rankN.json`。保留 `installed-rankN-<pid>.json` 中的探针 SHA 和进程身份，排除旧容器快照。

### 如何判断真实持有

- 每个目标 session 的 `holds_kv=true`，`committed` 至少覆盖真实输入；ACK/停止 token 可能多占少量 token。用 `sum(committed)` 报告 FULL 逻辑历史长度。
- `session_held_full_tokens` 按该版实现只计 tree lock 之外的 session-owned 尾部，**不是整条历史长度**。需要连同 `full_protected_size`、allocator free 和完整 slot mapping 解读。
- `.npz` 保存完整的页序列和 allocator 空闲页；若 offset 不符合页内顺序则另存完整映射。核对 SHA256、页内 offset、每流页唯一、流间页不别名、held 页不出现在空闲列表。
- 四 rank 应拥有相同的 FULL 逻辑页映射；各 TP shard 的 KV 内容和 hash 不要求相同。TP4 的同一逻辑历史也不能简单乘四成为“38.4M 上下文”。
- 本 checkpoint 的历史存储实际暴露 ratio1/2 的 KV 和 INDEX-K backing。检查每个样本的 components、字节数和非零字节，而非允许空列表互相比相等。
- 查询前后比较的是同一流、同一历史位置、同一 backing 的抽样。页面尾部追加或合法 SWA 重建可能改变邻近字节，应保留原始信息并解释，不能随意放宽对照或只挑稳定样本。
- `events-rankN.jsonl` 应显示 query 对原历史的匹配。结合响应 `cached_tokens_details.device` 与 `num_retractions`。小量页对齐尾部重算需明确计数，大段/全段重新 prefill 不算驻留通过。

这里证明的是逻辑所有权、已有 backing 的抽样和实际请求复用，再结合系统内存判断稳定性；**不是逐个 Linux 物理页驻留探测，也不是检查 960 万 token 的所有浮点元素**。`torch.cuda.memory_allocated/reserved` 不能单独作为每流物理驻留证据。

## 资源保护、错误与恢复

在四台主机分别启动 memory watcher，传入准确容器名和全新路径。prefill1 候选显式使用 `--interval 0.5`；原轮为 2 秒。输出 available GiB、容器 swap、OOM events、主机 pswpin/pswpout 和 pressure。在线中止线是容器 swap >0、新 OOM、可用内存连续三次低于 4GiB，或约 30 秒内至少三次增长且累计 >=64MiB swap-out。历史 swap 占用与本轮增量分开。现场四台 Linux page size 均为 4096 bytes，因此 `pswpout` 增量乘 4 得 KiB；其他平台先核对页大小。

例如在独立受管理会话中启动 head 监测（其余 rank 替换为当地实际容器名；`--root` 必须是尚不存在的目录）：

```bash
python3 -u switch/qualification/overrides/memory_watch.py \
  --container luz-switch-head \
  --root /path/to/new/resource-rank0 \
  --interval 0.5
```

watcher 只写 `fatal.json`，不负责终止模型。独立控制端必须持续检查它并执行本次请求的取消/停止与恢复；本轮主循环间隔为 3 秒，RPC 和四 rank 快照耗时另计。0.5 秒采样不代表 0.5 秒内一定停止，也不能只启动 watcher 后无人处理 fatal。主动结束监测可在其 root 中创建 `stop` 文件，再收集 summary 和完整 JSONL；停止采样不等于关闭 session。

**在线中止线不等于阶段签收条件。** 原轮 C8 只有一次低内存采样，没有触发连续三次保护，却仍未达到资源余量要求。每一级必须在全部屏障完成后，从四 rank 原始序列独立计算完整阶段的最低可用量；任一采样低于 4GiB、容器 swap 或 OOM 增量就不得签收和升档。0.5 秒采样仍不能证明采样间不存在更短峰值，所以同时保留查询 CUDA 峰值计数、内核事件和原始序列。已归档的稳定 C4 与失败 C8 可分别回归此 gate 的接受和拒绝分支。

补收 GPU/统一内存信息和本轮时间范围内的 kernel Xid；NVML 不支持的 UMA 指标应标为不可用。普通 health HTTP 200 不保证请求正在推进。已观察到无前向时部分 Prometheus queue/session gauge 落后，应对照实时 `/v1/loads`、snapshot 时间和请求进展。

还必须同时检查控制器 `status` 与每台资源样本的 `epoch`。本轮首次 prefill1 C16 曾在一流填充后因 SSH 监测请求超时中断，不能沿用旧 `progress` 和旧 `latest.json` 继续报告“正在填充、资源正常”。修正后的控制仅为 Timeout/连接中断类读取允许一次重试（命令读取超时设为 20 秒，SSH 连接/握手另有独立超时）；恢复后立即重读四机，签收样本新鲜度 <=10 秒。脚本错误、权限错误、持续通信失败、stale telemetry、资源 fatal 或探针错误均停止。它不扩大容量/数值资格，也不允许用重试掩盖 OOM。完整本地主机采样仍须收回复算，不能用成功重连替代。

runner 记录自己的 request ID 前缀。异常取消应向 native `/abort_request` 仅提交该唯一前缀，随后关闭自己的 session；不要用 `abort_all` 干扰其他用户。若 allocator 或分布式进程失效，按原部署流程停止四 rank、恢复已保存 runtime、重启并验健康。不要保留测试开关冒充原基线。

按 C1/2/4/8/12/16 推进，每级单独关闭与验收。本次用户后来将默认目标改为 luz-switch，因此收尾应保持原 B20 正式配置运行，hybrid_v2 停止备用；这是本次操作选择，不是所有使用者必须遵守的默认策略。

## 本轮结果和离线复核

实际结果见 [KV-RECALL](../reports/KV-RECALL.md)。可在不启动服务、不使用 GPU 的情况下复核公开的完整页序列和合成回答：

```bash
python3 switch/qualification/verify_kv_pages.py \
  --evidence switch/reports/kv-recall-20261015 \
  --receipt /path/to/new/page-audit.json
```

依赖 NumPy。该复核检查已归档证据，不会替代现场 GPU 推理或 Linux 物理页检测。会话修正是本次固定 native 请求测试用的 opt-in 模块；未完成 grammar/LoRA/优先级抢占等生产混合调度的完整资格，不应不经验证就加入一般生产配置。
