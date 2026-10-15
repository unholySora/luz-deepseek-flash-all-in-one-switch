"""Byte-level semantic and mutable-graph gate for the opt-in bridge address reuse."""
import argparse
import hashlib
import importlib.util
import json
import os
import pathlib
import statistics
import sys
import torch

sys.path.insert(0, '/opt/dsv41/adapter')
import kv_fp4_repack as runtime_bridge


def load(path, name='kv_fp4_repack_dedup'):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rows(scratch, indices):
    raw = scratch.as_strided((scratch.shape[0]*37440,), (1,))
    ids = indices.reshape(-1).long()
    data = (ids//64)*37440 + (ids%64)*576
    footer = (ids//64)*37440 + 36864 + (ids%64)*8
    return torch.cat((raw[data[:,None]+torch.arange(576,device='cuda')],
                      raw[footer[:,None]+torch.arange(8,device='cuda')]), dim=1)


def timing(fn):
    for _ in range(3):
        fn()
    graph = torch.cuda.CUDAGraph()
    with torch.cuda.graph(graph):
        fn()
    samples = []
    for _ in range(31):
        begin, end = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
        begin.record()
        graph.replay()
        end.record()
        end.synchronize()
        samples.append(begin.elapsed_time(end)*1000)
    return statistics.median(samples)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--override', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--hash', action='store_true')
    p.add_argument('--c8-plan', action='store_true')
    p.add_argument('--stock-source')
    a = p.parse_args()
    dest = pathlib.Path(a.output)
    dest.mkdir(parents=True, exist_ok=True)
    (dest/'client-source.py').write_bytes(pathlib.Path(__file__).read_bytes())
    (dest/'candidate-source.py').write_bytes(pathlib.Path(a.override).read_bytes())
    global stock
    stock = load(a.stock_source,'kv_fp4_repack_stock_audited') if a.stock_source else runtime_bridge
    if a.stock_source:
        assert hashlib.sha256(pathlib.Path(a.stock_source).read_bytes()).hexdigest()=='4867daaf5796f28ba47691c70ee82a60d4316e50ad60a99839d9dccef8811c79'
    stock._cuda_repack_mod = runtime_bridge._repack_cuda_lib()
    candidate = load(a.override)
    # Test file lives in /tmp, while the installed file resolves /opt/.../lib.
    # Use the exact installed baseline binary for auto dispatch in this harness.
    candidate._cuda_repack_mod = stock._repack_cuda_lib()
    os.environ['DSV41_FP4KV_REPACK_BACKEND'] = 'auto'
    os.environ['LUZ_SWITCH_FP4_BRIDGE_DEDUP'] = '1'
    os.environ['LUZ_SWITCH_FP4_BRIDGE_HASH'] = '1' if a.hash else '0'
    torch.manual_seed(1681)
    result = {'program_sha256': hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),
              'stock_source_sha256':hashlib.sha256(pathlib.Path(a.stock_source or runtime_bridge.__file__).read_bytes()).hexdigest(),
              'override_sha256': hashlib.sha256(pathlib.Path(a.override).read_bytes()).hexdigest(),
              'diagnostic_only': True, 'hash_mode': a.hash, 'c8_plan':a.c8_plan, 'fixtures': [], 'mutable_graph_checks': 0}
    num_pages = 400 if a.hash else 8
    shapes=[(1,7),(1,4096),(2,7),(5,31),(6,512),(10,128),(12,512),(24,512),(40,512),(48,512),(96,512),(97,7),(97,512)]
    if a.hash:
        shapes=[(1,7),(1,4096),(2,7),(5,31),(6,512),(10,128),(12,512),(18,512),(23,512),(24,512),(30,512),(36,512),(40,512),(42,512),(48,512),(60,512),(64,512),(65,512),(96,512),(97,7),(97,512)]
        shapes=[(24,512),(48,512)]+[shape for shape in shapes if shape not in [(24,512),(48,512)]]
    patterns=['shared','partial','unrelated','negative']
    if a.hash:
        patterns=['shared','scrambled','partial','unrelated','negative','constant']
    for pbs in [128, 256]:
        # Real block-planar pages, with nonzero storage offset and page padding.
        page_bytes = pbs*288 + 576
        owner = torch.empty(num_pages*page_bytes+32, dtype=torch.uint8, device='cuda')
        pool = owner[16:16+num_pages*page_bytes].view(num_pages,page_bytes)
        pool.random_(0,256)
        scales = pool[:, pbs*256:pbs*288]
        scales[(scales==127)|(scales==255)] = 56
        for m, topk in shapes:
            for pattern in patterns:
                anchor = torch.randint(0,num_pages*pbs,(m,topk),dtype=torch.int32,device='cuda')
                group = 5 if m%5==0 and m%6!=0 else 6
                ids = anchor[(torch.arange(m,device='cuda')//group)*group].clone()
                if pattern == 'partial':
                    ids[:,1::3] = anchor[:,1::3]
                elif pattern == 'scrambled':
                    ids=ids.gather(1,torch.rand(m,topk,device='cuda').argsort(dim=1))
                elif pattern == 'unrelated':
                    if a.hash:
                        ids=torch.randperm(num_pages*pbs,device='cuda')[:m*topk].view(m,topk).to(torch.int32)
                    else:
                        ids.copy_(anchor)
                elif pattern == 'negative':
                    ids.fill_(-1)
                elif pattern == 'constant':
                    ids.fill_(0)
                ids[:,::11] = -1
                # int64 and non-contiguous inputs exercise wrapper normalization.
                ids64 = torch.empty((m,topk*2),dtype=torch.int64,device='cuda')[:,::2]
                ids64.copy_(ids)
                reference, identity = stock.repack_fp4_extra_to_v4_ref(pool, ids, pbs)
                expected = rows(reference, identity)
                output, remap = candidate.repack_fp4_extra_to_v4(pool, ids64, pbs)
                actual = rows(output, remap)
                assert torch.equal(actual, expected), (pbs,m,topk,pattern,'reference mismatch')
                assert torch.equal(ids64.to(torch.int32),ids), 'Bridge mutated input indices'
                reused = int((remap.reshape(-1) != torch.arange(ids.numel(),device='cuda')).sum())
                valid_reused = int(((remap != torch.arange(ids.numel(),device='cuda').view_as(ids)) & (ids>=0)).sum())
                if a.hash and (48 if a.c8_plan else 24)<=m<=96 and topk==512:
                    if pattern in ['shared','scrambled','constant']:
                        assert valid_reused>int((ids>=0).sum())*.5, 'Hash failed to reuse valid slots'
                    if pattern=='unrelated':
                        assert valid_reused==0, 'Distinct physical slots must never alias'
                if pattern == 'shared' and 2 <= m <= 96:
                    if not a.c8_plan or m<=12 or m>=48 and topk==512:
                        assert reused > 0
                    else:
                        assert reused==0
                if m in (1,97):
                    assert reused == 0
                if m==48 and pattern=='partial':
                    os.environ['LUZ_SWITCH_FP4_BRIDGE_DEDUP']='0'
                    passthrough,passthrough_ids=candidate.repack_fp4_extra_to_v4(pool,ids,pbs)
                    assert torch.equal(rows(passthrough,passthrough_ids),expected)
                    assert torch.equal(passthrough_ids.reshape(-1),torch.arange(ids.numel(),device='cuda',dtype=torch.int32))
                    assert candidate._active_backend=='cuda'
                    os.environ['LUZ_SWITCH_FP4_BRIDGE_DEDUP']='1'
                stock_us = timing(lambda: stock.repack_fp4_extra_to_v4(pool,ids,pbs))
                stock_backend = stock._active_backend
                if ids.numel() >= 2048:
                    assert stock_backend == 'cuda', 'Production auto comparator must use installed CUDA backend'
                candidate_us = timing(lambda: candidate.repack_fp4_extra_to_v4(pool,ids,pbs))
                if m in (6,48) and pattern == 'partial' or a.hash and m==48 and pattern=='scrambled':
                    graph = torch.cuda.CUDAGraph()
                    with torch.cuda.graph(graph):
                        captured_scratch,captured_remap = candidate.repack_fp4_extra_to_v4(pool,ids,pbs)
                    # Toggle completely unrelated addresses, then shared addresses;
                    # stale remaps or skipped writes must become visible here.
                    for mutable in [anchor, anchor[(torch.arange(m,device='cuda')//group)*group]]:
                        ids.copy_(mutable)
                        graph.replay()
                        torch.cuda.synchronize()
                        reference,identity = stock.repack_fp4_extra_to_v4_ref(pool,ids,pbs)
                        assert torch.equal(rows(captured_scratch,captured_remap),rows(reference,identity))
                        result['mutable_graph_checks'] += 1
                    del graph
                result['fixtures'].append({'pbs':pbs,'M':m,'topk':topk,'routing':pattern,
                    'byte_equal':True,'reused_rows':reused,'valid_reused_rows':valid_reused,'rows':ids.numel(),
                    'stock_auto_us':stock_us,'stock_backend':stock_backend,'dedup_us':candidate_us})
                print(json.dumps(result['fixtures'][-1]),flush=True)
                (dest/'progress.json').write_text(json.dumps(result,indent=2))
    if a.hash:
        # Deliberately overfill a 32-entry table. The bounded probe failure
        # must convert original rows, preserving bytes instead of hanging.
        capacity=candidate._bridge_hash_capacity
        candidate._bridge_hash_capacity=lambda n:32
        ids=torch.arange(48*512,device='cuda',dtype=torch.int32).view(48,512)
        for trial in range(2):
            ids.copy_(torch.randperm(num_pages*pbs,device='cuda')[:ids.numel()].view_as(ids).to(torch.int32))
            reference,identity=stock.repack_fp4_extra_to_v4_ref(pool,ids,pbs)
            actual,remap=candidate.repack_fp4_extra_to_v4(pool,ids,pbs)
            assert torch.equal(rows(actual,remap),rows(reference,identity)), 'Full table fallback mismatch'
        candidate._bridge_hash_capacity=capacity
        result['forced_full_table_fallback_checks']=2
    result['passed'] = True
    (dest/'result.json').write_text(json.dumps(result,indent=2))
    print(json.dumps({'passed':True,'fixtures':len(result['fixtures']),
                      'mutable_graph_checks':result['mutable_graph_checks']}),flush=True)


if __name__=='__main__':
    main()
