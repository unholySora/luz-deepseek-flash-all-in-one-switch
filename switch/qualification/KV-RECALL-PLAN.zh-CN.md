# 全量 KV 驻留与召回验收方案

可以用现成小说填长上下文，但小说只适合作为背景。情节题有训练记忆污染，不能证明模型读取了当前 KV；一条 600k 通过，也不能证明 9.6M 池全部可用。

本方案准备了公版《三國志演義》背景 + 每条流独立随机记录 + streaming-session 保留 KV 的测试输入。**首次发布时仅完成离线准备；后续实际执行、各级签收和限制见 [KV 实测报告](../reports/KV-RECALL.md)。** 使用者后来将默认目标改为 luz-switch，收尾保留原 B20；具体监测与两个 opt-in 会话修正见 [监测部署指导](KV-MONITOR-GUIDE.zh-CN.md)。

## 语料与已准备的数据

原文来自 [Project Gutenberg #23950](https://www.gutenberg.org/ebooks/23950)，作者羅貫中，页面标明 public domain in USA；使用其旧小说原文，不使用现代改写、译本或注释。下载文件保留 Gutenberg 的头尾许可说明。正文从 START/END 标记之间截取供模型填充，来源和处理 SHA 可追溯。

| 项目 | 本次离线结果 |
|---|---|
| 原始 UTF-8 文件 | 1,863,656 bytes |
| 原始文件 SHA256 | `ed422e10c16c19af7ca7b09ca0320012e60776b3d7cb054769b8f7d9a19aea7d` |
| 提取正文 | 622,115 字符；当前 tokenizer 下 **558,403 tokens** |
| 正文 SHA256 | `cbd24f76abe2318b32617cd9660b3ad2f2c91ad8416a97d76724ec6cb082bd2b` |
| Tokenizer SHA256 | `c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b`；与服务模型挂载文件匹配 |
| C16 输入集 | 每条约 596.9k；合计 **9,550,940 input tokens**，为逻辑池的 **99.489%** |
| 核验点 | 每条 7 个，约位于 1%、10%、25%、50%、75%、90%、99%；共 **112 个** |

一部小说正文还不到 600k token，生成器会旋转正文起点并接续少量重复段；800k 档重复更多。清单记录每流重复比例和实际核验点 token 位置，不能称为 16 部独立小说。重复背景不影响随机记录的不可预知性，但不能代替多语料泛化测试。

每条流的最前方有不同 nonce，防止长前缀共享导致物理 KV 只存一份。各流使用相同的七个记录 key、**不同的随机值**，便于明确识别从其他流串入的值。每次正式运行重新生成随机 seed。问题只给 key，不给答案。填充阶段只回答 ACK，不提前把正确答案复制到上下文尾部。

## 三项验收需要分别成立

1. **容量与驻留**：同一时刻，多流的目标 KV 已写入并保持驻留，pool/page/slot 计数真实，没有靠回收或重新 prefill 轮流容纳。
2. **召回正确性**：所有流对不同位置随机记录的 exact match、漏答和串流情况可复算。
3. **系统稳定性**：四 rank 无 OOM、持续换出、死锁、请求错误或静默回退，关闭 session 后资源回收。

这类抽样召回不是逐一校验 960 万个 token 的所有 KV 浮点元素。它与已做的 kernel 数值对照、功能用例相互补充，不能互相替代。也不等同于 C16 decode 性能矩阵；这里不追加完整 16 并发速度测试。

## 为什么使用 streaming session

简单依次发送 16 条 600k 请求，前面的请求可能已经完成并被逐出缓存；16 个线程同时发请求也可能有相同问题。客户端慢读 SSE 不保证 GPU 停止生成。仅看到“请求总 token 数超过 9.6M”或 radix-cache hit，均不足以证明满池。

本次读取了实测镜像内的实际源文件：

| 路径（SGLang srt 下） | SHA256 | 已确认内容 |
|---|---|---|
| `session/session_controller.py` | `3dffcf40893a3b0ba175bd2c9d3d0678c12c4307641e8b2e3107a787e661e1ab` | `streaming=True`、session 关闭与超时回收路径 |
| `session/streaming_session.py` | `bf73c5866c1d592673a58f9e5a5d9627d0586cfefbaab48d9796e5e6992a20dc` | 完成请求后将 KV/lock/slot 保存；提供 session-held token/slot 计数；close 释放 |
| `managers/io_struct.py` | `c1a1863e431b84836ece23a3a5550c562e34148c771811064a81c810ecd68554` | `/open_session` 和 native `/generate` 的请求字段 |

因此可先向 16 个 streaming session 各填一条背景，全部保留后再发召回问题。`/open_session` 的 `capacity_of_str_len` 是字符容量参数，**不能当成实际分配的 token 数**。源码存在该机制，不代表当前模型组合已经通过运行验收；必须先做下面的小规模 smoke check。

## 逐级执行顺序

建议在专用 LuZ 测试窗口运行，四 rank 使用相同配置。每一级新建输出目录、输入 seed 和 sessions；收集该级全部数据并关闭后，再开始下一级。不要同时跑性能基准、镜像压缩或其他模型。

| 阶段 | 输入/并发 | 目的 |
|---|---|---|
| API smoke | 2 路，每路 2k | 验证 ACK 自然结束、后续 `/generate` 追加、JSON 召回、session 真正保留及 close 回收 |
| 位置基线 | 1 路；32k →128k →约597k | 将长度导致的模型召回误差与容量问题分开 |
| 占用阶梯 | 约597k × C2/C4/C8/C12/C16 | 找到独立 KV 保留与设备复用的容量边界；每一级先过容量、召回与资源门槛，逻辑页与 backing 抽样不等同逐物理页扫描 |
| 临界容量 | 前级通过后再试约599k × C16 | 接近 9.6M 上限，保留输出和 allocator 余量；不以精确填满所有 token 为目标 |
| 重复与对照 | 至少 3 个随机 seed，部分调整位置 | 报告最差流、各位置召回；必要时比较 B20 与 B32 |

`597000 ×16 =9.552M` 只是目标输入，实测准备结果为 9.55094M。距离 pool 上限约 49k，留给 ACK、追加问题、输出、page rounding 和投机暂存等。最终是否足够仍须看 allocator。`600000×16` 已用完全部池和上下文预算，无法同时容纳输出，不能作为有意义的请求参数。

hybrid_v2 的对应档是 **800k context / C10 /8M pool**：可将输入先设 797k、streams10、context800000、pool8000000；沿 C1/2/4/8/10 阶梯验收。不要将其 8M 池与 LuZ 9.6M 池的原始并发成绩直接混为同容量比较。

## 准备输入

下面命令都从仓库根目录执行。下载/生成结果放在忽略目录下，文件夹必须尚未存在。工具使用本地 tokenizer，不会下载模型权重。

```bash
python3 switch/qualification/prepare_kv_recall.py fetch \
  --output switch/corpus/three-kingdoms-001
python3 -m pip install tokenizers
python3 switch/qualification/prepare_kv_recall.py prepare \
  --novel switch/corpus/three-kingdoms-001/pg23950.txt \
  --tokenizer /srv/models/DeepSeek-V4.1-Flash-UL/tokenizer.json \
  --streams 16 --input-tokens 597000 --context 600000 --pool 9600000 \
  --output switch/recall-runs/prepared-c16-001
```

保留 `manifest.json`：seed、tokenizer/encoder/source SHA、各流输入 SHA、真实 token 数和插入位置。`streamNN.json` 包含 native `input_ids`、追加问题 token 和本地评分用答案；**不能把 expected 字段送给模型**。生成器采用仓库内的 DeepSeek-V4 chat encoder，禁用 thinking；不是裸小说 completion。服务必须使用匹配 tokenizer 和协议。

小规模 smoke 的 prepare 参数改为 `--streams 2 --input-tokens 2048`；context/pool 仍填实际服务的值。每个完整请求和后续轮次均不得超过该服务 context。

## 执行召回（需先配好独立驻留监测）

```bash
python3 switch/qualification/run_kv_recall.py \
  --suite switch/recall-runs/prepared-smoke-001 \
  --base http://127.0.0.1:8006 --expect-running 16 \
  --output switch/recall-runs/smoke-result-001 --hold-seconds 30
```

API key 可通过进程环境 `LUZ_RECALL_API_KEY` 提供，脚本不保存 key。smoke 通过且 all-rank 监测已就绪后，才把 suite 改为相应阶梯。runner 不自动切换模型、不修改引擎、不 flush 整个服务缓存、不执行重试。

脚本逐流填充，要求 ACK 自然停止且 `prompt_tokens` 与准备值一致；然后保留所有 sessions，同时查询七个 key。输出原始响应、fill/plateau/close 的 metrics、逐流评分和 cleanup。无论成功或异常，都会请求关闭自己创建的 session。异常中仍在推理的请求可能触发服务端延迟 close，必须等释放指标归零后结束测试窗口。

**runner 的 `summary.json` 固定保留 `full_pool_qualified=false`**：它只负责 API 召回，不能从客户端证明 GPU page 所有权。满池结论必须结合独立的驻留证据另行签收，不由一次 HTTP 200 自动升级。

## 驻留监测要采什么

在全部填充完成、第一条召回发出之前，以及召回后、close 后，至少各有一次**四 rank 同期快照**：

- 活跃 session ID、session-held req count、各 slot 的 committed/allocated KV 长度、page mapping 及其摘要；检查同一 rank 内不同流的应独立区间没有非法 page 别名。
- tree protected/evictable 与 session-held 的分别计数，以及总 free/used/full/SWA/extra pool 计数。避免把同一 tree lock 范围重复相加；TP4 不应把同一逻辑 token 简单乘四。
- 每次 follow-up 的 cached/prefill token、retract、evict、prefix miss。若保留的 session 被重算，该轮不能证明满池驻留。
- 主机 `MemAvailable`、`/proc/vmstat` 的 `pswpin/pswpout` **增量**、memory pressure、容器 `memory.events`/swap、GPU/统一内存指标与 Xid；四 rank 各自记录。

现成 `/metrics` 不一定暴露 session slot 细节。先确认实际指标语义；缺失时需增加诊断专用的只读快照/采样入口，而不能用 `token_usage` 单一比率代替。不要为了测试把 scheduler/cache 算法改掉后继续声称是在验收原 B20。

容量阶梯配置保守停止条件：任一 rank 新增 OOM、持续 swap-out、session 丢失、retract、page 分配失败，或可用内存持续低于预留线，即停止升档并释放本轮 session。可先把 4GiB 作为实验保护线，再依据 smoke 和实际 allocator 开销调整；它不是已验证的硬件安全保证。已有历史 swap 占用不等于本轮发生换出，应看增量。

B20 当前 600k 单流质量测试最低可用内存约 7.3GiB；这不能外推满池还够。B32 在短控制实验中多留约 6.6GiB，可作为容量对照，但其 FP4 extra-KV 还须独立完成满池召回，不能凭省内存即推荐默认切换。

## 评分与判定

必须同时报告：

- 记录 exact match（本例分母 112）、每条流 7/7 的比例、每个深度位置召回率、最差流；不能只给总体百分比。
- 缺失、值错误、其他流同 key 的值、JSON 格式错误、重复 key、输出到达 token 上限分别归档。评分器严格解析 JSON，拒绝重复 key。
- 各阶段真实 resident token/page 数、独立上下文数、冷 prefill 与保留 KV 后的 recall 用时；不把 prefix-hit 时间写成冷 prefill。
- 原始结果和资源门槛。失败不重试覆盖；如需复测，新开 run ID，保留失败轮。

本组随机记录建议以全部 112/112、零串流、零系统错误且驻留证明完整作为该 seed 的基础验收线；至少三 seed 后再报告范围。若固定位置在 C1 长文本时也错，先分析检索/提示/量化误差；若 C1 正确而满池后串值或出错，再查 page mapping、slot 重用、回收和内存压力。小说情节问答可另做附加用户体验评估，不能替代以上验收。
