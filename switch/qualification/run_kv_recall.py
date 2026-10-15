"""Run prepared pinned-session recall requests; never self-certify full residency.

Run only on a dedicated inference service after the streaming-session smoke
check and memory/residency monitor described in KV-RECALL-PLAN.zh-CN.md.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import pathlib
import time
import urllib.request
import uuid


def grade(expected, text, other_values):
    def no_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        answer = json.loads(text, object_pairs_hook=no_duplicate_keys)
        if not isinstance(answer, dict):
            raise ValueError("not an object")
    except (ValueError, TypeError):
        return {"json_valid": False, "correct": 0, "total": len(expected), "missing": len(expected),
                "wrong_stream": 0, "exact": False}
    correct = sum(answer.get(k) == v for k, v in expected.items())
    missing = sum(k not in answer for k in expected)
    wrong_stream = sum(isinstance(answer.get(k), str) and answer[k] in other_values.get(k, set())
                       for k in expected if answer.get(k) != expected[k])
    return {"json_valid": True, "correct": correct, "total": len(expected), "missing": missing,
            "wrong_stream": wrong_stream, "exact": answer == expected}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=pathlib.Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--expect-running", type=int, default=16)
    parser.add_argument("--key-env", default="LUZ_RECALL_API_KEY")
    parser.add_argument("--hold-seconds", type=int, default=0)
    args = parser.parse_args()
    manifest = json.loads((args.suite / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "luz-switch-kv-recall-v1"
    args.output.mkdir(parents=True, exist_ok=False)
    key = os.environ.get(args.key_env)

    def api(path, body=None, raw=False, timeout=1800):
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        req = urllib.request.Request(args.base.rstrip("/") + path,
                                     None if body is None else json.dumps(body).encode(), headers)
        # Use a separate opener per request so concurrent calls do not share
        # mutable transport state. Proxies are disabled for local cluster APIs.
        local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with local.open(req, timeout=timeout) as response:
            data = response.read().decode()
        return data if raw else json.loads(data)

    def write(name, data):
        (args.output / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def metrics(label):
        try:
            (args.output / (label + "-metrics.txt")).write_text(api("/metrics", raw=True, timeout=30), encoding="utf-8")
        except Exception as exc:
            write(label + "-metrics-error.json", {"error_type": type(exc).__name__})

    info = api("/server_info", timeout=30)
    selected = {k: info.get(k) for k in ["context_length", "max_total_num_tokens", "max_running_requests"]}
    assert selected == {"context_length": manifest["context"], "max_total_num_tokens": manifest["pool"],
                        "max_running_requests": args.expect_running}, selected
    write("server-info.json", selected)
    cases = []
    for row in manifest['cases']:
        data = (args.suite / row['file']).read_bytes()
        if hashlib.sha256(data).hexdigest() != row['sha256']:
            raise ValueError('Prepared request file checksum mismatch')
        cases.append(json.loads(data))
    prefix = "kv-recall-" + uuid.uuid4().hex
    opened = []
    records = []
    all_values = {}
    for case in cases:
        assert case["input_tokens"] == len(case["input_ids"])
        for k, v in case["expected"].items():
            all_values.setdefault(k, set()).add(v)
    started = time.time()
    try:
        metrics("before")
        for case in cases:
            sid = prefix + "-" + str(case["stream"])
            session = api("/open_session", {"session_id": sid, "capacity_of_str_len": case["text_chars"] + 100000,
                                            "streaming": True, "timeout": 10800}, timeout=30)
            session_ok = session == sid or (isinstance(session, dict) and session.get('session_id') == sid and session.get('success', True))
            if not session_ok:
                raise RuntimeError("Opening the streaming session failed")
            opened.append(sid)
            t0 = time.time()
            response = api("/generate", {"input_ids": case["input_ids"], "session_params": {"id": sid},
                                         "sampling_params": {"temperature": 0, "max_new_tokens": 32}, "stream": False})
            write(f"fill-{case['stream']:02d}.json", response)
            meta = response.get("meta_info", {})
            finish = meta.get("finish_reason", {})
            if response.get("text", "").strip() != "ACK" or not isinstance(finish, dict) or finish.get("type") != "stop":
                raise RuntimeError("Initial ACK did not finish naturally; do not use the suite as a recall score")
            if meta.get("prompt_tokens") != case["input_tokens"]:
                raise RuntimeError("Server tokenizer/input count disagrees with the prepared suite")
            metrics(f"fill-{case['stream']:02d}")
            print(json.dumps({"filled_sessions": len(opened), "fill_s": round(time.time() - t0, 3)}), flush=True)
        metrics("plateau-before-query")
        if args.hold_seconds:
            # Monitoring interval for an operator-owned independent all-rank
            # residency sampler. This client does not inspect GPU page ownership.
            time.sleep(args.hold_seconds)

        def query(pair):
            case, sid = pair
            t0 = time.time()
            response = api("/generate", {"input_ids": case["query_ids"], "session_params": {"id": sid},
                                         "sampling_params": {"temperature": 0, "max_new_tokens": 512}, "stream": False})
            write(f"query-{case['stream']:02d}.json", response)
            text = response.get("text", "")
            others = {k: values - {case["expected"][k]} for k, values in all_values.items()}
            result = {"stream": case["stream"], "elapsed_s": time.time() - t0,
                      "grade": grade(case["expected"], text, others), "meta_info": response.get("meta_info", {})}
            finish = result['meta_info'].get('finish_reason', {})
            result['output_limit_reached'] = isinstance(finish, dict) and finish.get('type') == 'length'
            return result

        with concurrent.futures.ThreadPoolExecutor(len(cases)) as executor:
            for record in executor.map(query, zip(cases, opened)):
                records.append(record)
                write("recall-records.json", records)
        metrics("plateau-after-query")
        correct = sum(r["grade"]["correct"] for r in records)
        total = sum(r["grade"]["total"] for r in records)
        summary = {"records_correct": correct, "records_total": total, "record_exact_match": correct / total,
                   "streams_all_correct": sum(r["grade"]["exact"] and not r['output_limit_reached'] for r in records), "streams": len(cases),
                   "wrong_stream_values": sum(r["grade"]["wrong_stream"] for r in records),
                   "json_format_errors": sum(not r['grade']['json_valid'] for r in records),
                   "missing_records": sum(r['grade']['missing'] for r in records),
                   "output_limit_streams": sum(r['output_limit_reached'] for r in records),
                   "planned_prompt_tokens": manifest["planned_prompt_tokens"], "elapsed_s": time.time() - started,
                   "residency_independently_verified": False, "full_pool_qualified": False,
                   "scope": "Recall results only. Review all-rank KV page ownership, retraction and memory samples before any capacity claim."}
        write("summary.json", summary)
        print(json.dumps(summary), flush=True)
    finally:
        cleanup = []
        for sid in opened:
            try:
                api("/close_session", {"session_id": sid}, raw=True, timeout=30)
                cleanup.append({"session": sid, "close_requested": True})
            except Exception as exc:
                cleanup.append({"session": sid, "close_requested": False, "error_type": type(exc).__name__})
        write("cleanup.json", cleanup)
        metrics("after-close")


if __name__ == "__main__":
    main()
