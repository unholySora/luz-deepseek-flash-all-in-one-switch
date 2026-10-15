# B20 后续优化方向与判据

2026-10-15。本文区分已有测量、源码确认和待验证假设。已有吞吐见 [RESULTS](RESULTS.md)，此前没有采纳的试验见 [上游报告](../UPSTREAM-REPORT.zh-CN.md)第 5 节。本轮 prefill1 内存候选的实际结果见 [KV 实测](KV-RECALL.md)；其余方向尚未产生新的业务吞吐成绩。

## 优先级

| 优先级 | 方向 | 依据 | 下一步可验收的工作 |
|---|---|---|---|
| P0 | 保留可验证的容量基线与改动回归 | 本轮独立测试配置近满池 C16 得到112/112、最低可用6.57GiB；原同批 prefill C8 仍保留资源失败 | 任何 KV/分块/入场改动均重跑 C1/2/4/8/12/16 的页图、历史 backing、查询复用、召回与资源检查；该结论不扩大原 B20 默认配置的 streaming 或速度资格 |
| P0（资格扩展） | 重复轮次、长时间持有及追加/取消 | 本轮为单 seed、逐流冷填充后同时查询；七个随机点不是整本小说理解或所有 KV 元素检查 | 多 seed 与不同背景，满池 idle/持续 decode 后重复查询，close/取消/重入，已有 slot 的分块长追加；保持页图和 backing 证据，单独报告正确性与资源失败 |
| P1 | MoE 的实际 routing 与执行计划 | 晚期 C8 code cycle 约 80.1ms，单独暴露 MoE 约 42.4ms；固定 cluster32 曾在 C4 受益、C2/C8 退步 | 先记录实际 M、每层/每阶段专家集合、专家负载分布和 materialization；比较针对形状的计划，避免引入 GPU→CPU 同步来做逐步选路 |
| P1（长输入负载） | 冷 prefill 的长度成本与 workspace | 本轮 131056-token 约 22.8s，596945-token 约 155.1s；原 C8 续写查询时 head 可用量低至 2.52GiB，召回正确但资源未通过 | 先验证 `--prefill-max-requests 1` 能否约束峰值，并保留 MRR16、9.6M 池和同时提交的客户端查询；随后按续写长度/批量/可用量做有界入场，兼顾短提示 batching 与 P95/P99 延迟 |
| P1（明确源码候选） | FP4 INDEX-K packed readback 融合 gather | 实际镜像的 `get_index_k_fp4` 构造 `[n,64]` int64 `payload_cols`，其字节数是最终 68-byte/row packed K 的约 7.53 倍；prefill 的 n 可为同批可见历史长度之和 | 用单个有界 gather 直接写出原 int8 payload/int32 scales，保持逐字节一致；先单/多请求、ratio1/2、页边界、空行、非法输入约束与 graph 检查，再测峰值和长 prefill。该候选尚未接入本轮服务 |
| P1（长历史查询） | 分离入场、续写 prefill 与稳态 decode | 已命中近597k设备历史的查询，prefill1 C8/C12 短回答完成中位仍约5.71/8.12s；含排队和陆续入场，不能当纯 decode | 先在 C1/C8 固定64k/128k/约600k背景，分别记录完整波次、最后一流入场后的窗口及 per-ratio indexer/attention/MoE/通信。短提示 MoE 占比不能直接解释长历史成本 |
| P1 | 用 structured v2 C4 定位每步差距 | 两端平均提交都约 5.988 token/verify，LuZ 聚合 414.6、hybrid_v2 445.9 tok/s，约低 7%；C8 则基本持平 | 固定同一输入和完整波次，双栈各采早/后期四 rank trace，分离实际 batch、MoE、dense、head、通信及入场成本；先解释差距再改参数 |
| P2 | 将 certified head 的资格扩至更大 batch | 当前白名单仅 M6/12/18/24/30/36/42/48；解析上限 MAX_M=128，不是已经获得 M60/72/96 资格 | 按实际 DSPARK padding 确认 M；四 TP shard 比较完整 head 与候选的 logits、argmax、stop mask、mutable graph；通过后才纳入白名单，并测净收益 |
| P2 | 根据 batch 与有效接受收益调节 draft/verify | prose 常约 3 token/verify，code 及新数字任务接近 6；反复统一增减 cap 未获得普遍胜出 | 在不增加同步的统计基础上做有限分档；计入 draft 成本、verify 成本和完整请求尾部，同时验证 TP 一致、停止语义和 graph 生命周期 |
| P2 | FP4 extra-KV 消费端减少转换 | B32 多留约 6.6GiB，但 C8 code 仍比 V4 慢；hash bridge 累计时间只比 raw FP4 少约 4% | 优先研究消费端直接读取、跨步有失效规则的复用、合并 hash/init/dequant；先逐字节、边界、graph 验证，再独立做长文召回 |
| P3 | 持续到达负载与 admission/尾部补位 | interval1 已改善受控长请求入场；整齐同时到达的短提示矩阵覆盖有限 | 固定到达轨迹，混合长 prefill、短 decode、自然停止与取消；同时记录首 token、P95/P99 完成时间、活动槽占用和总输出 |

P1 方向覆盖长输入临时内存、总体执行开销和可复现的双栈差距；优先级不代表已确认根因。若满池测试出现资源或复用失败，P0 高于其余速度工作。

## 本轮预检已经发现的具体问题

在 B20 加 `--enable-streaming-session` 的独立测试配置下，16 条约 2k 的会话全部完成 ACK 并持有自己的行后，后续 16 条查询出现 **running=0、waiting=16、request-row free=0**。此时 FULL 只使用 32,768 token，不是 9.6M 池不够。实际 allocator 已支持有 KV 的请求复用原行，但 scheduler 在恢复会话 KV 之前只按全局空闲行计算入场额度，因而挡住了这些合法 continuation。

新修正作为独立 opt-in 模块放在 `qualification/overrides/session_row_admission.py`，与只读驻留探针分开。它只在全局空闲行为零时，为拥有独立有效行、未中止、非 beam、且不与运行 batch 共用行的 continuation 开启入场；保留最大运行 batch 和实际 allocator。普通请求保持排队。10 项 CPU 合约检查覆盖了原版阻塞复现、16 行复用、混合队列、重复行、活动行、取消、beam 和异常恢复。GPU 会话及长文验收结果另行记录，不能用 CPU 检查替代。

同一次阻塞期间，`/v1/loads` 已报告 16 条等待，而 Prometheus 部分 queue/session gauge 仍停留在早先的值。因此监测还应增加“采样时间/前向是否推进”的语义，避免无前向时 gauge 看似空闲。当前实测先依靠 scheduler 内的四 rank 快照和实时 loads 互证，没有改动生产 metrics 热路径。

长度基线还发现首次 streaming 分块 prefill 跳过普通缓存维护，在约 125k 处用尽 125440-token SWA 池，FULL 仍有大量空闲。`session_chunk_cache.py` 为尚无 held slot 的首轮 chunk 恢复原生 cache 路径，CPU 7 项合约检查后，128k GPU 基线已通过 7/7 召回和页图复核。该变化与前面的 row admission 修正分别记录；已有 slot 的大幅长追加未追加资格测试。这两处都是本次启用 streaming-session 后暴露，不能据此解释此前普通请求的全部速度差异。

原 C8 同批查询虽为 56/56、无 retract/OOM/容器 swap，完整序列仍显示 head 一次可用内存 2.519GiB，主机 swap-out 约 85.9MiB。峰值落在约 3 秒的续写 prefill 窗口，而 plateau 到查询完成的常驻 CUDA allocation 只增加约 17MiB。证据支持优先调查短续写携带长历史时的临时 workspace；**尚未定位具体 kernel，不能直接断言是某个 BF16 gather**。当前镜像中读到的一个 sparse BF16 workspace 分支排除了 SM120，不能把它当作 GB10 已走路径。

因此追加独立的 prefill1 候选，同时将采样由 2 秒提高到 0.5 秒，并加入“阶段内任一点低于 4GiB 就不签收”的复核 gate。它不缩减 pool、context 或 MRR，也不通过提前 close 来腾容量。该候选的实际结果见 [KV 实测报告](KV-RECALL.md)；若有效，后续更细的入场策略应区分无历史短请求和长历史 continuation，不能直接把一次容量实验外推为所有负载的最佳默认。

prefill1 的 C8 复测已得到 56/56，输入总计与原轮相同的 4,775,645 token，四 rank 仍达到 8 个 running 请求。阶段最低可用量为 6.72GiB，头节点本级 swap-out 12KiB，容器 swap 和 OOM 增量均为 0；原轮对应最低 2.52GiB、头节点 swap-out 87,924KiB。查询完成中位从约 6.32s 到 5.71s，但这是不同启动轮中的一个短回答波次，诊断采样也不同，**不作为多轮业务速度胜出结论**。目前最具体的优化方向是约束长历史 continuation 的 prefill 临时内存，并进一步定位 workspace，保留短请求需要的 batching。

后续 C12 为84/84、最低6.48GiB；C16 为112/112，实际9,551,256输入 token，占9.6M池99.492%，四 rank 各自核验9,551,288 committed FULL token，查询 running 峰值16，最低可用6.57GiB。C16 各位置均16/16，无串值、容器 swap 或 OOM。C16 短回答完成中位约10.36s，含排队及续写 prefill，不是纯 decode 排名。首次 C16 管理通信中断与单独补跑均归档。单 seed、短回答及近满池后的短观察窗口，仍留有后续质量/稳态负载测试空间。

SWA 也需要与 FULL 分开看：C16 plateau 的 SWA allocator free 虽只有3,328/125,440，但有113,920 evictable，protected和session-tail各4,096；本轮查询后正常推进，close后保护和session持有归零。不能仅凭 free 较低判断已经耗尽，也不能只看 FULL 余量推断任意长追加都已验收。

### packed INDEX-K gather：已经确认的源码分配与尚待证明的因果

本轮从实际运行容器只读提取的 `sglang/srt/mem_cache/deepseek_v4_memory_pool.py`（SHA256 `37fe674ceebfc5f5b2f02d09c011045b391d378335f096932780949fb7581f3d`）中，`get_index_k_fp4` 用 `payload_cols=(off*64).unsqueeze(-1)+torch.arange(64)` 生成 int64 `[n,64]`，随后通过 advanced indexing 读取 packed payload；另建 `[n,4]` scale 列索引。该代码不只是读取最终 68-byte/row 数据，还会分配大尺寸的索引张量。

`v41_indexer/scoring.py::get_deep_gemm_prefill_data`（SHA256 `86ecc8fd977d0b336ddcdec5ba9eae453c0bdbf8d81c636358c9f95b8e0bbcf8`）将各请求的可见 K slots 拼接，再由 `full_topk.py` 调用 packed readback。若同一个 ratio1 prefill batch 覆盖本次 C8 全部 4,775,645 条历史 K 行，**payload_cols 一项就是 2.277GiB**，而最终 payload+scales 约 0.302GiB；单路约 596,945 行分别是 0.285GiB 与 0.038GiB。ratio2 应按实际压缩行数另算，不能套用 ratio1 数字。

这是形状和 dtype 直接决定的分配上界示例，**没有现场 allocation stack 证明原失败窗口恰好由 8 路一次进入该分支，也没有将 2.277GiB 当作已经测得的峰值贡献**。需要先确认真实 dispatch、batch、n 及生命周期，再做 fused gather A/B。这个方向只改变 packed 数据搬运，若实现正确可逐字节比较，通常比改量化格式更容易限定数值影响；仍须通过实际质量和 graph 验收，不能先承诺收益比例。

## 对收益的合理预期

MoE 类别占比较高，意味着应优先测实际专家访问与执行几何，不能将 42.4/80.1 直接当作可消除比例。分类存在重叠，当前“专家读带宽”也是估算，尚无硬件 DRAM 计数器支持。专家去重、DRAFT/VERIFY 数据复用是否可行，取决于实际集合重叠、精度、写入和 graph 生命周期。

长 prefill 的初步数据来自本次监测配置及小说背景，不能直接替换公开 PR 基准。当前 chunk=4096，dense-indexer logits budget=128MiB，而 `DSV41_PREFILL_EMPTY_CACHE_TOKENS=8192`；若试更大 chunk，需要同时记录是否跨过已有 allocator 清理阈值，避免把分块大小和清理行为混在一个“单变量”结果里。数据先用于划定要 profile 的长度区间，还没有确认能消除多少开销。

certified head 是一处明确的资格范围缺口，但“走了完整 head”不等于新路径一定更快。较大 M 下完整 GEMM 效率、筛选开销和候选分布会改变，需要比较端到端成本。C10 常见 M60、C12 常见 M72、C16 常见 M96；必须以实际 padded M 为准，不能只依据并发数推算。

FP4 的价值首先是容量余量。如果 B20 满池稳定、内存余量足够，继续使用较快的 V4 extra-KV 合理；如果内存是边界，B32 可以作为独立候选，但不能把短提示省内存直接当作满池质量通过。原生 FP4 INDEX-K 与 extra-KV 的量化格式用途不同，应分别记录。

## 暂不优先重复的工作

- 仅增大 RoCE gather/slot：4MiB 曾降低暴露 NCCL 时间，却使 C8 code 聚合试验约 500→472.5 tok/s，没有稳定净收益。链路接近双 100G 也不意味着小消息成本为零，但需要通信关键路径证据才值得再改。
- 一组固定 MoE cluster32：已有 114 项数值/graph 对照和完整业务试验，收益随 batch 反转。后续应针对真实 routing 条件专门化。
- 全局更激进 cap、BF16 draft head：此前未得到 prose/code 同时稳定提升；不重复用最快单次挑选默认。
- 将旧 structured 数到 200 后的强制续写当作内核回归：新 v2 已修正任务长度。新旧结果继续分开；真正 schema/grammar 解码另列测试，不能混称。

## 验证顺序

1. 保留 B20 为对照，冻结 prompt、分词器、输出预算、seed、温度和完整波次；诊断 profiler 数据单独归档。
2. 每次只改一个执行因素，先做数值/停止/graph 资格，再跑 C1 prose/code 及 C2/4/8 的完整请求，保留失败和未满输出。
3. 与容量相关的变化必须重跑独立 KV 驻留与召回；不能沿用另一种 KV 布局的通过结果。
4. 优化持续到达负载时同时看吞吐和尾延迟，不以一个指标提升覆盖另一个明显退步。

不因本文的候选方向改变本轮容量测试的算法参数。默认服务按使用者最新决定保持已验收的 luz-switch B20，hybrid_v2 停止备用。
