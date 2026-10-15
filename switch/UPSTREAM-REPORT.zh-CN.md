# 给 LuZ 上游的交换机移植、性能复核与未采纳方案报告

发布：`switch-v2026.10.15-b20`；日期：2026-10-15。上游比较基底固定为 `fb5798c6b73d543b297bc3ff410003549f5d96da`。本报告可供上游审阅；内容已去除实际网络、账号、平台授权和运行容器身份。

## 1. 结果与比较边界

本次将四台GB10的LuZ部署移植到交换机双rail网络，完成初始化修复、原生INDEX-K与V4 extra-KV页适配、WO-A预取窗口、flat RoCE、head白名单和调度入场调整。原始prefill差距并没有在同口径复测中成立；decode初测的明显落后主要与实际初始化/回退路径及每次verify的有效输出有关。

B20保持600000总上下文、MRR16、实际9600000 token KV池，完成C1/2/4/8四场景三轮180条完整2048输出，另通过C8功能与单路599440-token召回。原速度版本没有完整C16吞吐。后续独立[KV驻留与召回实测](reports/KV-RECALL.md)记录多路长文结果、两个会话运行时缺陷及修正，以及 prefill 每批 1 请求的内存候选；未用该测试扩大原版的速度或 streaming-session 资格。

本次生产式试验使用固定自定义镜像及本地UL checkpoint；上游文档描述的发布栈、环网拓扑与本站环境并不完全相同。因此相对文档的差异是跨现场复核，不能被解释为“只更换交换机”的单变量收益。脱敏构建模板亦未以新构建镜像重新做线上性能验收。

## 2. 相对原仓库文档的公开口径表现

原文来源：[README](https://github.com/luxingcom/luz-deepseek-flash-all-in-one/blob/fb5798c6b73d543b297bc3ff410003549f5d96da/README.md)、[量具定义](https://github.com/luxingcom/luz-deepseek-flash-all-in-one/blob/fb5798c6b73d543b297bc3ff410003549f5d96da/docs/registry/BENCHMARK-SOURCE.md)。V4.1主模型DE锚为81.67，FL6现役锚82.17；README中的80.93 prose、123.26 code及structured C8 79.21属于0731兼容模型，不能直接拿来作为V4.1的三场景对照。

### 2.1 不改动公开sd_bench的DE，max400、自然停止、每类5次

| 场景 | 原V4.1文档锚 | B20中位tok/s | B20五次实际输出 | 相对现役锚 |
|---|---:|---:|---|---:|
| prose | 82.17（另有81.67历史锚） | 80.77 | 400,400,400,400,400 | -1.70% |
| code | 未找到可直接配对的V4.1 code锚 | 138.94 | 400,400,400,400,400 | 不计算 |

公开工具SHA256：`060884e418483dbd2cae742d07da20aa4b132d2a50c06f0c7d719ca2ee816f3c`。初始移植A0用同一公开量具的prose为73.70；第一轮A31复测82.09，已接近现役锚。上表另补当前B20结果，避免用旧候选替代最终候选。公开code工具实际使用binary_search，不与SD-1的clamp50或clamp200混算。

### 2.2 同一公开PR、repeated filler、实际prompt_tokens/TTFT

| 目标档位 | 原文档tok/s | B20三次仲裁中位tok/s | 变化 |
|---:|---:|---:|---:|
| 8192 | 5203.7 | 5964.8 | +14.63% |
| 32768 | 6082.2 | 6870.1 | +12.95% |
| 65536 | 6126.3 | 6935.7 | +13.21% |
| 131072 | 5883.3 | 6645.5 | +12.96% |

每档first、warm及三次轮换仲裁独立保存，表内只取后三次中位。重复填充享有Engram行缓存特性；不能外推为任意业务文档的prefill速率。原文档的agg600k=4958.5为5×200k另一尺度，本次未按该尺度重跑，不拿单路近600k召回墙钟替代。

### 2.3 完整2048输出与hybrid_v2同提示交叉控制

LuZ B20为600k/C16/9.6M；对照hybrid_v2为800k/C10/8M，各保留当前配置。独立原始提示C1 prose 82.29 vs81.39，code200 144.85 vs145.21；其它nonce矩阵窗口的prose也有小幅反向波动。证据不支持C1两场景稳定全面胜出。

| 场景 | C | LuZ单路/聚合tok/s | hybrid_v2单路/聚合tok/s | 配对完整流 |
|---|---:|---:|---:|---:|
| prose | 1 | 83.77 / 83.8 | 84.34 / 84.3 | 3/3 |
| prose | 2 | 60.84 / 119.1 | 59.17 / 117.0 | 6/6 |
| prose | 4 | 41.25 / 160.9 | 40.98 / 160.4 | 12/12 |
| prose | 8 | 27.02 / 208.0 | 27.28 / 212.4 | 24/24 |
| code | 1 | 138.81 / 138.8 | 136.69 / 136.7 | 3/3 |
| code | 2 | 100.27 / 198.2 | 97.85 / 186.8 | 6/6 |
| code | 4 | 81.12 / 312.5 | 82.27 / 322.5 | 12/12 |
| code | 8 | 65.93 / 501.0 | 70.22 / 543.0 | 16/24 |

hybrid_v2控制90条中89条满2048，一条C8 code为1941、finish_reason=stop、无请求错误。未重跑替换；跨栈排名对称排除双方该整波，C8 code只有两波16对。LuZ原始24条完整统计另存，不以23条对24条的混合选择宣布胜负。

## 3. 交换机化的实际修改与原因

| 修改 | 现场原因 | 实现与验证 | 取舍 |
|---|---|---|---|
| switched物理peer与proxy | 双rail交换机上所有rank可直达，环网opposite/two-wave假设不一定合适 | RoCE proxy C适配；保留通信健康检查与双HCA；实测约196Gb/s链路 | 大包带宽正常不等于小消息集体通信没有成本 |
| flat/one-shot集体通信选择门 | 只设B12X_ROCE_TOPOLOGY=switched仍可能导入roce_ring模块 | 明确选择镜像已有one-shot runtime，保留pynccl资格、graph生命周期与健康检查；44项rank/shape求和/gather/graph对照通过 | 默认all-reduce/gather均512KiB，扩大阈值未稳定更快 |
| INDEX-K初始化与dense planner恢复 | 此SM121/SGLang组合中，memory-pool先命中kv_fp4分支，后续elif内的sparse-wire/page64安装被遮住；sparse kernel回退DenseBlocks后dense planner又受抑制 | 在实际命中分支先安装INDEX-K page64；仅对SM12x、ratio1/2和page64恢复对应metadata；13项CPU分支回归、16项GPU oracle | 属于本站组合的实际命中缺陷，不声称所有上游环境均触发 |
| INDEX-K与extra-KV开关分离 | 原生FP4 INDEX-K和R1 FP4 extra-KV不是同一个格式/用途 | INDEX-K保留原生FP4；速度候选extra-KV用V4 | 避免把关闭extra-KV FP4误作关闭原生indexer |
| V4 extra-KV物理page64 | 原page128在本FlashInfer prefill组合不受支持 | 只改低ratio1/2物理页，FULL逻辑页256和slot换算不变；22项页/工厂/边界例通过 | 约多用7–8GiB，需另做长文与资源验收 |
| WO-A独立L2预取窗口 | collective窗口不能覆盖linear→WO-A所需的数据时序 | 移植独立窗口，保留流间join；WO-A诊断耗时约1.85–1.99→1.06ms | 局部kernel收益不等于端到端相同比例提升 |
| C1–C8 certified head白名单 | 仅M6会让C2–C8回退完整head | M=6/12/18/24/30/36/42/48；四TP shard共96例，对logits/argmax/值/stop mask/graph核验 | 未扩称C16资格 |
| 运行噪声与内存 | debug、频繁打印、生成式health及过大Engram host cache影响有限UMA/延迟 | 关闭cert debug、health生成；Engram每层0.5GiB、malloc arena2；保留真实功能验收 | 是部署条件变化，不全计作数值kernel收益 |
| B20 cap与入场间隔 | draft收益依场景；interval16延迟已有decode期间的新prefill受理 | cap0.3→0.15；interval16→1；相对A31其他字段语义一致 | 后续43k新请求完成约12.6→7.2s，已有decode近似；并非所有负载无代价 |

915个选定构建输入中只有RoCE proxy C与上游工作树不同，这个统计仅描述那三个目录的构建源码；它不代表运行栈只有一处修改。五个只读运行覆盖、环境变量、基础镜像依赖及可选B32覆盖都另列清单。

## 4. structured旧口径的扩展效率问题及新口径

旧SD-1 structured要求数1到200，所有输出先完成相同691字符序列，随后ignore_eos/min_tokens强制续到2048，出现HTML、代码或说明文本。C8平均提交4.51–5.06 token/请求verify，code约5.54–5.63；最早与最晚流结束差8.9–18.8s，活动槽利用率75.4%–85.8%，显著低于code约95%–96%。首token错位约0.33s不足以解释全部差异。

按使用者要求，新structured v2令计数上限随输出预算变化：2048-token输出要求数到2048，完整数字任务经tokenizer确认需要5144 token，使输出在数字任务未完成时耗尽预算。新旧口径独立保存；没有用改提示后的高分冒充引擎加速。

| C | LuZ旧v1聚合 | LuZ新v2单路/聚合 | hybrid_v2新v2单路/聚合 |
|---:|---:|---:|---:|
| 1 | 142.4 | 164.16 / 164.2 | 164.15 / 164.1 |
| 2 | 201.8 | 128.52 / 254.6 | 127.06 / 249.9 |
| 4 | 252.0 | 104.93 / 414.6 | 111.49 / 445.9 |
| 8 | 299.9 | 85.63 / 670.7 | 84.85 / 669.1 |

数字计数不等于grammar或schema引擎的性能。完整数字连续性、格式与各波accept/尾部数据见[STRUCTURED](reports/STRUCTURED.md)。

补充同B20旧提示C8早/后期trace，四rank各32对DRAFT/VERIFY：头节点cycle约70.3→90.2ms，单独暴露MoE约33.4→49.5ms；dense约5.92→6.07ms、RoCE约6.75→7.09ms，CPU after-pair gap约0.145→0.148ms。后段计算成本也增加，主要变化落在MoE类别；这仍是阶段关联，不是已经测得专家集合或DRAM根因。

## 5. 已尝试但未作为默认采用的方向

| 方向 | 做过的工作与观察 | 未采纳原因/保留条件 |
|---|---|---|
| 更大的RoCE slot/gather | 第一轮2MiB、第二轮4MiB；数值和graph门通过；4MiB使暴露NCCL约1.31→0.38ms | RoCE本身成本增加，C8 code聚合试验约500→472.5，无一致端到端收益；保留512KiB |
| 一组固定32-cluster MoE计划 | 24个routing几何、114项数值与graph对照；只改已测六个形状，再测56条完整请求 | C4 code提高，C2/C8下降；不存在普遍更快计划；保留原MoE计划 |
| raw FP4 extra-KV | 与V4保持同容量控制，62条完整PC输出 | 省约7GiB但C8 prose/code约下降4.6%/8.4%；速度默认不采用 |
| B31列内去重 | 104字节夹具和8 mutable graph；共享微基准M48约72.8→27.5µs | 真实索引与共享夹具不同，晚期累计桥接甚至更慢；不能外推微基准 |
| B32跨列hash | 252夹具、12 graph、2强制满表fallback；C8只读覆盖；业务和长文检查通过 | 额外保留约6.6GiB，但C8 code仍慢于V4；桥接累计只比raw FP4少约4%，因此仅作可选内存配置 |
| BF16 draft head | 单变量试验保留完整输出与对照 | 未得到prose/code同时提升；额外开销与接受长度收益未形成净优势 |
| folded sampling强制组合 | 第一轮实际服务控制 | 约80.73/133.42，未胜过对应候选；不以局部代码路径更短替代端到端判断 |
| cap0.5/分位置cap/统一更激进cap | 多轮冻结prompt控制，所有候选保留 | 接受长度和每步代价相互抵消，未确认跨场景稳定优胜；cap0.15只是当前有限候选 |
| 为draft增加stop-word数值掩码 | 只读观察15995候选，23个被禁止，占0.144% | prose为0、code也很少，不足解释主要差距；没有据此修改采样数值 |
| 增加更大上下文/C16容量结论 | 池初始化、近600k单流与C8短提示分别验证 | 缺完整16路长文占满证据，不能从池数值推导已通过满载 |
| 以profile吞吐、最快单次或截掉尾段宣布胜出 | trace、uncapped观察和分段速度均保留 | profiler扰动、输入任务差异和强制续写会扭曲比较；正式排名保留完整请求和共同波次 |

这些方向中，B32已以明确边界作为可选配置交付；“未作为默认”不等于删除成果。更大gather、MoE32、其他cap与诊断源仍留在本地研究归档，没有塞入默认启动路径。

## 6. GPU证据对下一步优先级的影响

B20晚期C8 code头节点GPU cycle中位约80.1ms，单独暴露MoE约42.4ms、dense5.9ms、RoCE7.1ms、NCCL2.2ms，GPU cycle间隙约0.15ms。没有发现可通过普通CPU调度补掉的几十毫秒空洞。kernel时间有重叠，不能相加或直接做Amdahl估算。

更值得继续研究：实际routing下专家集合和materialization复用、按batch与有效接受收益调整draft工作量、在消费端减少FP4→V4转换，以及持续到达负载中的尾部补位。优先补逐请求/分阶段accept、cap、routing集合和真实DRAM计数，再做优化；当前专家读带宽为模型估算，尚非硬件计数器。

## 7. 质量、容量与复现材料

- [极详细部署指导](DEPLOYMENT-GUIDE.zh-CN.md)：硬件映射、双rail/QoS、镜像、权重与Engram、SSH、逐rank配置、启停、验收和回退。
- [性能与prefill数据](reports/RESULTS.md)、[structured分析及新结果](reports/STRUCTURED.md)。
- `profiles/`：脱敏模板；`overrides/manifest.json`：实测只读代码的确切SHA。
- `bench/`：旧SD-1、公开sd_bench、structured v2与冻结提示；原始数据不因结果不利而丢弃。
- `qualification/package-verification.json`：四rank算法参数与原试验语义一致性，及915源码/6覆盖字节核验。
- `images/`：镜像身份及单独镜像包checksum，镜像文件不传GitHub。
- [镜像增量与重建说明](image-delta/README.zh-CN.md)：完整复用基础71层，实测只新增5个约5.4MB压缩层；GitHub发布脱敏增量，另加小型host-key清理层。分发版index不同，计算代码未改；Docker load与CPU检查通过，未重跑GPU矩阵。
- [满池KV召回方案](qualification/KV-RECALL-PLAN.zh-CN.md)和[实际执行报告](reports/KV-RECALL.md)：公版小说加每流随机记录，四 rank 完整逻辑页映射与 backing 抽样、device cache 复用、串值、逐位置召回和内存。另有[监测与修正部署指导](qualification/KV-MONITOR-GUIDE.zh-CN.md)。

未重跑上游GSM8K/qeval的完整题库，不声称语言质量分数全面等同。本站数值用例、功能检查、单路长文召回与C8速度各自说明对应范围。首次发布收尾恢复hybrid_v2；同日下午使用者明确要求默认改为luz-switch，因此后续KV测试收尾恢复原B20正式配置并保持运行，hybrid_v2停止备用。两个session测试修正和prefill1容量候选均未混入默认生产配置。
