#!/usr/bin/env python3
"""Recompute the published benchmark statistics without contacting a model."""
import collections
import hashlib
import json
import math
from pathlib import Path
import re
import statistics

ROOT = Path(__file__).resolve().parent
REPORT = ROOT.parent / "FIRMWARE-PERFORMANCE-20261022.zh-CN.md"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def close(actual, expected):
    assert math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-9), (
        actual, expected
    )


def de_stats(files):
    trials = [read(p)["de"] for p in files]
    assert all(len(rows) == 5 for rows in trials)
    for rows in trials:
        for row in rows:
            assert len(row["completion"]) == row["conc"] == len(row["per_all"])
            assert all(isinstance(v, int) and v > 1 for v in row["completion"])
            assert row["agg"] > 0 and all(v > 0 for v in row["per_all"])
            # sparkDash uses the upper middle observation for an even batch.
            close(row["per"], sorted(row["per_all"])[row["conc"] // 2])
    lengths = [v for rows in trials for r in rows for v in r["completion"]]
    trial_per = [statistics.median(r["per"] for r in rows) for rows in trials]
    trial_agg = [statistics.median(r["agg"] for r in rows) for rows in trials]
    return {
        "per": statistics.median(trial_per),
        "aggregate": statistics.median(trial_agg),
        "requests": len(lengths),
        "short": sum(v < 400 for v in lengths),
        "histogram": {str(k): v for k, v in sorted(collections.Counter(lengths).items())},
        "trial_per": trial_per,
        "trial_aggregate": trial_agg,
    }


def main():
    sources = read(ROOT / "sources.json")
    summary = read(ROOT / "summary.json")
    comparison = read(ROOT / "comparison.json")
    same = read(ROOT / "same-capacity-20261021.json")
    for row in sources["files"]:
        assert hashlib.sha256((ROOT / row["path"]).read_bytes()).hexdigest() == row["sha256"], row["path"]
    assert len(sources["files"]) == 119
    assert len(list((ROOT / "raw").rglob("*.json"))) == 118
    instrument = (ROOT / "benchmark/sd_bench.py").read_bytes()
    assert hashlib.sha256(instrument).hexdigest() == comparison["new_script_sha256"]
    assert hashlib.sha256(instrument.replace(b"\n", b"\r\n")).hexdigest() == comparison["old_script_sha256"]
    report = REPORT.read_text(encoding="utf-8")
    request_counts = collections.Counter()
    ratios = []
    for scene in ["prose", "code", "json", "structured"]:
        for concurrency in [1, 2, 4, 8]:
            key = f"{scene}-c{concurrency}"
            before = de_stats([ROOT / f"raw/old-20261020/de-{key}.json"])
            after = de_stats([ROOT / f"raw/new-20261022/de-{key}-t{t}.json" for t in [1, 2, 3]])
            published = comparison["DE"][key]
            current = summary["DE"][key]
            for prefix, stats in [("old", before), ("new", after)]:
                request_counts[prefix] += stats["requests"]
                close(stats["per"], published[f"{prefix}_per"])
                close(stats["aggregate"], published[f"{prefix}_aggregate"])
                assert stats["requests"] == published[f"{prefix}_requests"]
                assert stats["short"] == published[f"{prefix}_short"]
                assert stats["histogram"] == published[f"{prefix}_completion_histogram"]
            close(current["aggregate_median"], after["aggregate"])
            close(current["per_stream_median"], after["per"])
            assert current["requests"] == after["requests"]
            assert current["short_completions"] == after["short"]
            assert current["completion_histogram"] == after["histogram"]
            for t, item in enumerate(current["trials"]):
                close(item["aggregate_median"], after["trial_aggregate"][t])
                close(item["per_stream_median"], after["trial_per"][t])
                assert (ROOT / item["source_file"]).is_file()
            per_delta = 100 * (after["per"] / before["per"] - 1)
            agg_delta = 100 * (after["aggregate"] / before["aggregate"] - 1)
            close(published["per_delta_pct"], per_delta)
            close(published["aggregate_delta_pct"], agg_delta)
            ratios.append(after["aggregate"] / before["aggregate"])
            label = scene + ("*" if before["short"] or after["short"] else "")
            table_row = (
                f"| {label} | C{concurrency} | {before['per']:.2f} | {after['per']:.2f} | "
                f"{per_delta:+.2f}% | {before['aggregate']:.2f} | {after['aggregate']:.2f} | {agg_delta:+.2f}% |"
            )
            assert table_row in report, table_row
            if scene == "structured":
                assert {k: v / before["requests"] for k, v in before["histogram"].items()} == {
                    k: v / after["requests"] for k, v in after["histogram"].items()
                }
            if concurrency <= 2:
                aux = de_stats([ROOT / f"raw/same-capacity-20261021/de-{key}-t{t}.json" for t in [1, 2, 3]])
                close(aux["aggregate"], same["DE"][key]["aggregate_median"])
                close(aux["per"], same["DE"][key]["per_stream_median"])
                assert aux["short"] == same["DE"][key]["short_completions"]
    assert request_counts == {"old": 300, "new": 900}
    assert summary["protocol"]["DE_rounds"] == 240
    assert summary["protocol"]["DE_requests"] == 900
    close(100 * (math.exp(statistics.mean(math.log(v) for v in ratios)) - 1), comparison["aggregate_change_geometric_mean_pct"])
    close(100 * (min(ratios) - 1), comparison["aggregate_change_min_pct"])
    close(100 * (max(ratios) - 1), comparison["aggregate_change_max_pct"])
    for size, published in comparison["PR"].items():
        before = [read(ROOT / f"raw/old-20261020/pr-{size}-{t}.json")["pr"][0] for t in [1, 2, 3]]
        close(statistics.median(r["tok_s"] for r in before), published["old_tok_s"])
        close(statistics.median(r["ttft_s"] for r in before), published["old_TTFT_s"])
        if size not in summary["PR"]:
            assert published["new_tok_s"] is None and published["new_TTFT_s"] is None
            assert published["delta_pct"] is None
            continue
        after = [read(ROOT / f"raw/new-20261022/pr-{size}-t{t}.json")["pr"][0] for t in [1, 2, 3]]
        close(statistics.median(r["tok_s"] for r in after), published["new_tok_s"])
        close(statistics.median(r["ttft_s"] for r in after), published["new_TTFT_s"])
        close(published["new_tok_s"], summary["PR"][size]["median_tok_s"])
        close(100 * (published["new_tok_s"] / published["old_tok_s"] - 1), published["delta_pct"])
        assert after == summary["PR"][size]["samples"]
    assert not comparison["causal_firmware_gain_proven"]
    assert not summary["firmware_activation_complete_at_measurement"]
    assert sum(c["matches"] for n in summary["firmware_at_measurement"] for c in n["components"]) == 10
    assert set(summary["PD_active_version_unconfirmed_at_measurement"]) == {"rank0", "rank3"}
    for window in summary["resources"].values():
        for node in window:
            assert node["max_cgroup_swap_bytes"] == node["oom_kill_delta"] == 0
    assert report.count("```") % 2 == 0
    for target in re.findall(r"\]\(([^)]+)\)", report):
        if not target.startswith(("https://", "http://", "#")):
            assert (REPORT.parent / target.split("#")[0]).exists(), target
    checksums = ROOT / "SHA256SUMS"
    assert checksums.exists()
    for line in checksums.read_text().splitlines():
        expected, relative = line.split("  ", 1)
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected, relative
    print("PASS: 118 raw JSON files; 16 DE comparisons; 900 new requests / 240 rounds; "
          "7 old and 3 new PR sizes; C1/C2 auxiliary baseline; source/report hashes and links.")


if __name__ == "__main__":
    main()
