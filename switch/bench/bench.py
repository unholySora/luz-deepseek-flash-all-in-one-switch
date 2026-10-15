"""Observe the current service with the archived SD-1 four-prompt decode matrix.

The reference protocol is imported unchanged. Added evidence distinguishes a
transport success from a full 2048-token stream; partial waves remain visible.
No server configuration, cache, API key or repetition guard is changed.
"""
import argparse
import concurrent.futures
import datetime
import fcntl
import hashlib
import importlib.util
import json
import pathlib
import statistics
import threading
import time
import urllib.error
import urllib.request

P = argparse.ArgumentParser(description=__doc__)
P.add_argument('--base', default='http://127.0.0.1:8009')
P.add_argument('--model', default='hybrid_v2')
P.add_argument('--output', required=True)
P.add_argument('--run-tag', required=True)
P.add_argument('--waves', type=int, default=3)
P.add_argument('--max-tokens', type=int, default=2048)
P.add_argument('--types', default='prose,code')
P.add_argument('--concs', default='1')
P.add_argument('--paired-suite')
P.add_argument('--expect-context', type=int)
P.add_argument('--expect-running', type=int)
P.add_argument('--expect-pool', type=int)
P.add_argument('--deadline', default=(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)).isoformat())
P.add_argument('--prompt-tag', default='round2-frozen-20261015')
A = P.parse_args()
OUT = pathlib.Path(A.output)
OUT.mkdir(parents=True, exist_ok=True)
assert not (OUT / 'status.json').exists(), 'Use a fresh run directory'
SOURCE = pathlib.Path(__file__).parent / 'reference-hybrid-sd1/sd_protocol.py'
assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == '4dd30c18be565b87403266a81acd12d2e9c95e814dcae4ea00e363e3c3e99edb'
spec = importlib.util.spec_from_file_location('hybrid_sd1', SOURCE)
sd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sd)
BASE = A.base.rstrip('/')
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
TYPES = A.types.split(',')
CONCS = [int(s) for s in A.concs.split(',')]
SUITE = json.loads(pathlib.Path(A.paired_suite).read_text()) if A.paired_suite else None
MATRIX_SOURCE = pathlib.Path(__file__).parent / 'matrix-prompts.json'
MATRIX_SUITE = json.loads(MATRIX_SOURCE.read_text())
DEADLINE = datetime.datetime.fromisoformat(A.deadline).timestamp()
assert set(TYPES) <= set(sd.PROMPTS) and all(c in [1, 2, 4, 8] for c in CONCS)
STOP = threading.Event()


def dump(name, data):
    path = OUT / name
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def mark(phase, **details):
    row = dict(phase=phase, epoch=time.time(), **details)
    dump('status.json', row)
    print(json.dumps(row, ensure_ascii=False), flush=True)


def get(path, timeout=15):
    with OP.open(BASE + path, timeout=timeout) as r:
        return r.read()


def metrics():
    return [line for line in get('/metrics').decode().splitlines()
            if line and not line.startswith('#') and any(s in line for s in (
                'spec', 'num_running_reqs', 'num_queue_reqs', 'num_used_tokens',
                'token_usage', 'cache', 'retract', 'generation_tokens_total',
                'prompt_tokens_total', 'kv_'))]


def idle(rows):
    vals = [float(line.rsplit(' ', 1)[1]) for line in rows
            if line.startswith(('sglang:num_running_reqs{', 'sglang:num_queue_reqs{'))]
    assert len(vals) >= 2, 'Missing scheduler running/queue metrics'
    return all(v == 0 for v in vals)


def wait_idle():
    deadline = time.monotonic() + 300
    while True:
        rows = metrics()
        if idle(rows):
            return rows
        if time.monotonic() > deadline:
            raise RuntimeError('Service did not become idle; no requests aborted')
        time.sleep(2)


def sample():
    with (OUT / 'metrics-timeline.jsonl').open('a') as f:
        while not STOP.is_set():
            row = {'epoch': time.time()}
            try:
                row['metrics'] = metrics()
                mi = dict(line.split(':', 1) for line in pathlib.Path('/proc/meminfo').read_text().splitlines())
                row['host_mem_available_kib'] = int(mi['MemAvailable'].split()[0])
                row['host_swap_used_kib'] = int(mi['SwapTotal'].split()[0]) - int(mi['SwapFree'].split()[0])
            except Exception as e:
                row['error'] = repr(e)
            f.write(json.dumps(row) + '\n')
            f.flush()
            STOP.wait(2)


def request(ptype, conc, index, wave, *, budget=None, barrier=None):
    budget = A.max_tokens if budget is None else budget
    assert time.time() < DEADLINE, 'Deadline reached; no new request launched'
    prompt = SUITE[ptype][wave % len(SUITE[ptype])] if SUITE and ptype in SUITE and wave >= 0 and conc == 1 else MATRIX_SUITE[f'{ptype}/c{conc}/w{wave}/r{index}'] if wave >= 0 else sd.build_prompt(ptype, conc, index, A.run_tag)
    body = sd.chat_body(A.model, prompt, budget, force_fill=budget == A.max_tokens)
    rec = sd._blank(len(prompt), index, wave, ptype, conc)
    rec.update(prompt=prompt, prompt_sha16=hashlib.sha256(prompt.encode()).hexdigest()[:16],
               requested_output_tokens=budget, content_events=0, done_received=False)
    req = urllib.request.Request(BASE + '/v1/chat/completions', json.dumps(body).encode(),
                                 {'Content-Type': 'application/json'})
    if barrier:
        barrier.wait(timeout=30)
    t0 = time.time()
    first = last = None
    ct = pt = None
    pieces = []
    output_events = []
    cumulative_chars = 0
    gaps = []
    try:
        with OP.open(req, timeout=min(900, max(1, DEADLINE - time.time()))) as resp:
            for raw in resp:
                if time.time() >= DEADLINE:
                    raise TimeoutError('Research deadline reached; stream closed for service restoration')
                line = raw.strip()
                if not line.startswith(b'data:'):
                    continue
                payload = line[5:].strip()
                if payload == b'[DONE]':
                    rec['done_received'] = True
                    break
                ev = json.loads(payload)
                rec['request_id'] = ev.get('id', rec.get('request_id'))
                if ev.get('error'):
                    rec['error'] = ev['error']
                    break
                if ev.get('usage'):
                    rec['server_usage'] = ev['usage']
                    ct = ev['usage'].get('completion_tokens')
                    pt = ev['usage'].get('prompt_tokens')
                for ch in ev.get('choices', []):
                    if ch.get('finish_reason') is not None:
                        rec['finish_reason'] = ch['finish_reason']
                    d = ch.get('delta') or {}
                    piece = (d.get('content') or '') + (d.get('reasoning_content') or '')
                    if piece:
                        now = time.time()
                        if first is None:
                            first = now
                        if last is not None:
                            gaps.append(now - last)
                        last = now
                        pieces.append(piece)
                        cumulative_chars += len(piece)
                        output_events.append([now, cumulative_chars])
                        rec['content_events'] += 1
    except urllib.error.HTTPError as e:
        rec['error'] = 'HTTP %d %s' % (e.code, e.read()[:1000].decode(errors='replace'))
    except Exception as e:
        rec['error'] = repr(e)
    end = time.time()
    text = ''.join(pieces)
    rec = sd._finish(rec, t0, first, last, end, ct, pt, len(text))
    if not rec['done_received'] and not rec.get('error'):
        rec['error'] = 'Stream ended without [DONE]'
    rec.update(output_text=text, full_length=not rec.get('error') and ct == budget,
               output_events=output_events,
               max_content_gap_s=max(gaps, default=None))
    return rec


def cell(waves, raw, ptype, conc):
    result = sd.cell_summary(waves, raw, ptype=ptype, conc=conc,
                             max_tokens=A.max_tokens, run_tag=A.run_tag)
    rows = [r for wave in raw for r in wave]
    full = [r for r in rows if r['full_length'] and r.get('decode_tps')]
    valid_waves = [w for w, rs in zip(waves, raw)
                   if len(rs) == conc and all(r['full_length'] for r in rs)]
    result.update(streams_requested=conc * A.waves, streams_full=len(full),
                  streams_short=sum(not r.get('error') and not r['full_length'] for r in rows),
                  streams_error=sum(bool(r.get('error')) for r in rows),
                  full_length_waves=len(valid_waves),
                  median_decode_tps_full=statistics.median(r['decode_tps'] for r in full) if full else None,
                  agg_decode_tps_full_waves=statistics.median(w['agg_decode_tps'] for w in valid_waves) if valid_waves else None,
                  completion_tokens_range=[min(r['completion_tokens'] for r in rows), max(r['completion_tokens'] for r in rows)],
                  waves_detail_all=waves)
    return result


def main():
    # A shared, nonblocking advisory lock only guards other copies of this client.
    lock = open(pathlib.Path(__file__).parent / 'decode-matrix.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    mark('preflight')
    info = json.loads(get('/server_info'))
    for key in ('api_key', 'admin_api_key', 'ssl_keyfile_password'):
        info.pop(key, None)
    for field, expected in [('context_length', A.expect_context), ('max_running_requests', A.expect_running), ('max_total_num_tokens', A.expect_pool)]:
        if expected is not None:
            assert info[field] == expected, (field, info[field], expected)
    dump('server-info-before.json', info)
    dump('metrics-before.json', wait_idle())
    meta = dict(protocol_id=sd.PROTOCOL_ID, protocol=sd.PROTOCOL_DOC, model=A.model,
                base=BASE, run_tag=A.run_tag, types=TYPES, concurrencies=CONCS,
                max_tokens=A.max_tokens, waves=A.waves,
                started=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                protocol_sha256=hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
                client_sha256=hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),
                queue_before_each_wave='wait until running=queue=0; no flush',
                repetition_guard='unchanged', cell_order='type, then concurrency, then wave',
                launch='per-wave thread barrier', paired_suite=bool(SUITE), prompt_tag=A.prompt_tag, matrix_suite_sha256=hashlib.sha256(MATRIX_SOURCE.read_bytes()).hexdigest(),
                paired_suite_sha256=hashlib.sha256(pathlib.Path(A.paired_suite).read_bytes()).hexdigest() if SUITE else None,
                scope='SD1 timing with frozen C1 prompts and fixed cross-variant multi-C nonces',
                context=info['context_length'], actual_pool=info['max_total_num_tokens'], service_running_limit=info['max_running_requests'])
    results = {'_meta': meta}
    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    try:
        for ptype in TYPES:
            mark('warmup', type=ptype)
            wait_idle()
            warm = request(ptype, 1, 0, -1, budget=32)
            dump('warmup-' + ptype + '.json', warm)
            for conc in CONCS:
                waves, raw = [], []
                for w in range(A.waves):
                    mark('wave', type=ptype, conc=conc, wave=w)
                    before = wait_idle()
                    barrier = threading.Barrier(conc)
                    with concurrent.futures.ThreadPoolExecutor(max_workers=conc) as ex:
                        futures = [ex.submit(request, ptype, conc, i, w, barrier=barrier) for i in range(conc)]
                        recs = [f.result() for f in futures]
                    after = metrics()
                    dump('wave-%s-c%d-w%d.json' % (ptype, conc, w),
                         dict(records=recs, metrics_before=before, metrics_after=after))
                    raw.append(recs)
                    waves.append(sd.aggregate_wave(recs, conc))
                    print(json.dumps(dict(type=ptype, conc=conc, wave=w,
                                          full=sum(r['full_length'] for r in recs),
                                          tokens=[r['completion_tokens'] for r in recs],
                                          median=waves[-1].get('median_decode_tps'),
                                          aggregate=waves[-1].get('agg_decode_tps'))), flush=True)
                result = cell(waves, raw, ptype, conc)
                results['DE-V3_%s_C%d' % (ptype, conc)] = result
                dump('de_%s_c%d.json' % (ptype, conc), raw)
                dump('de_v3_matrix.json', results)
                mark('cell_done', type=ptype, conc=conc,
                     single=result.get('median_decode_tps_full'),
                     aggregate=result.get('agg_decode_tps_full_waves'),
                     full=result['streams_full'], requested=result['streams_requested'])
        dump('metrics-after.json', wait_idle())
        info_after = json.loads(get('/server_info'))
        for key in ('api_key', 'admin_api_key', 'ssl_keyfile_password'):
            info_after.pop(key, None)
        dump('server-info-after.json', info_after)
        assert all(info[k] == info_after[k] for k in ('context_length', 'max_running_requests', 'max_total_num_tokens', 'chunked_prefill_size'))
        dump('complete.json', dict(complete=True, cells=len(TYPES) * len(CONCS), requests=A.waves * sum(CONCS) * len(TYPES),
                                   all_full=all(c['streams_full'] == c['streams_requested'] for k, c in results.items() if k != '_meta'),
                                   completed=datetime.datetime.now(datetime.timezone.utc).isoformat()))
        mark('complete', cells=len(TYPES) * len(CONCS))
    finally:
        STOP.set()
        thread.join(timeout=20)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        mark('failed', error=repr(e))
        raise
