# 四项调优与后续方向

2026-10-15，四台 GB10，交换机双 100G RoCE；链路聚合实测约 196Gb/s。容量保持 600k/C16/9.6M，速度测试到 C8。诊断 profiler 的吞吐不进入正式排名。

| 调优项 | 完成的工作 | 结论 |
|---|---|---|
| Draft 单位成本 | uncapped 观察、cap0.3/0.2/0.15 的冻结提示控制 | prose 平均每次提交约 3 token，code200 接近全接受；保留 0.15，但 C1 双场景稳定优势未证实 |
| C2/C4/C8 | 512KiB/4MiB gather、四 rank 数值和 graph 验证、逐并发复测 | 4MiB gather 的 NCCL 暴露时间变短，但总吞吐没有一致改善，保留 512KiB |
| MoE/dense/调度 | 114 项 MoE plan 数值与 graph 对照、精确 shape 试验、GPU 关键路径 | 改 MoE plan 收益随场景变化；保留原计划。prefill 入场 interval1 对受控混合负载有效，后续长请求完成时间从约 12.6s 缩至 7.2s |
| KV 内存 | V4/FP4、列内复用、跨列 C8 hash repack | B32 通过 252 个字节对照、12 个 mutable graph 和 2 个强制 fallback；可多留约 6.6GiB 头节点内存，但 decode 仍慢于 V4 |

B20 的 C8 late-code GPU cycle 中位约 80.1ms，其中单独暴露 MoE 约 42.4ms，dense 约 5.9ms、RoCE 约 7.1ms、NCCL 约 2.2ms。GPU cycle 间隙约 0.15ms，没有发现几十毫秒的 CPU 断粮。分类包含重叠部分，不能简单相加或据此计算 Amdahl 上限。MoE 专家读取模型给出的 GB/s 是估算，尚非硬件 DRAM 计数器。

## 下一步优先顺序

1. **先修正 structured 的负载定义并定位尾部。** 保留原 SD-1 结果作历史基线，另建持续数字序列、自然停止、真实 schema 三个独立测试；记录逐请求 verify 次数、accept/cap 和专家集合。不能用改提示后更好看的数字覆盖旧结果。
2. **按接受率和 batch 专门化 draft/verify 成本。** code 接近 6 token/verify、prose 约 3，统一 cap 很难同时最佳。先做在线统计与受控分档，验证停止规则、TP 一致性、采样正确性和 graph 复用，再考虑自适应策略。
3. **针对实际 MoE 路由集合和长尾压缩开销。** 优先测专家集合去重、materialization 和 DRAFT/VERIFY 是否重复读取；以真实 routing shape 为输入。上一轮固定 cluster32 没有普遍胜出，不能直接设成默认。
4. **省内存路径继续削减 repack 与 launch。** B32 相比原始 FP4 bridge 的累计 kernel 时间只减约 4%，尚不足抵消转换。优先研究在消费端直接使用 FP4、跨步可验证复用或合并 hash/init/dequant；这些均属后续假设，未作为已完成优化。

网络链路已接近双 100G 总速率，继续只调 NCCL 环境变量的收益优先级低于专家访存、投机单位收益和长尾调度。保留交换机的优点是所有 rank 可双 rail 直达；继承环网专用 two-wave 假设会抵消这一点，因此本版本使用 switched/flat 路径。

structured v2 提供了更干净的下一轮参照：两端C8每次请求verify平均提交约5.988 token，数字连续性全部正确；C8聚合670.7 vs669.1基本相当，C4却是414.6 vs445.9，LuZ仍低约7%。建议下一轮先对这个C4固定输入做双栈GPU trace，分离实际batch、MoE计划、dense路径和冷态入场成本。接受率几乎一致时，它比直接反复调随机散文更适合定位每步执行成本。该对照尚未实施，不算本版本已取得收益。

## 验收边界

180 条完整 2048-token 四场景输出验证速度稳定性；另有 9 项功能检查和单路 599440-token 三处召回。没有验证 16 条长上下文同时填满 9.6M。B32 的数值、graph 和有限业务检查不能等同于对所有 prompt 的数学等价证明。

收尾控制器曾因嵌入式监控脚本缩进报错恢复默认服务，修复后重启同配置完成剩余质量和 prefill；已完成速度结果未重复挑选。hybrid_v2 新鲜对照有一条 C8 code 在 1941 token 以 stop 结束，保留该记录，并在跨栈排名中对称排除双方对应整波。
