# -*- coding: utf-8 -*-
"""임베딩 후보 비교: Hit@K / MRR. Dense · BM25 · Hybrid(RRF)."""
import json, re, sys, time, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from golden_qa import QA

MODELS = [
    # (id, 표기, query prefix, passage prefix, trust_remote_code)
    ("BAAI/bge-m3",                        "bge-m3",            "",         "",           False),
    ("Alibaba-NLP/gte-multilingual-base",  "gte-multi-base",    "",         "",           True),
    ("intfloat/multilingual-e5-large",     "e5-large(512ctx)",  "query: ",  "passage: ",  False),
    ("sentence-transformers/all-MiniLM-L6-v2", "MiniLM(하한)",  "",         "",           False),
]
K = 5

def load():
    C = [json.loads(l) for l in open(pathlib.Path(__file__).parent / "chunks.jsonl")]
    gold = []
    for qa in QA:
        ids = {c["chunk_id"] for c in C
               if c["tech_id"] == qa["tech"] and re.search(qa["gold"], c["text"])}
        gold.append(ids)
    return C, gold

def metrics(ranked_lists, gold):
    h1 = h3 = h5 = 0; mrr = 0.0
    for ranked, g in zip(ranked_lists, gold):
        pos = next((i for i, cid in enumerate(ranked) if cid in g), None)
        if pos is None: continue
        if pos < 1: h1 += 1
        if pos < 3: h3 += 1
        if pos < 5: h5 += 1
        mrr += 1.0 / (pos + 1)
    n = len(gold)
    return dict(hit1=h1/n, hit3=h3/n, hit5=h5/n, mrr=mrr/n)

def subset(ranked_lists, gold, idxs):
    return metrics([ranked_lists[i] for i in idxs], [gold[i] for i in idxs])

def rrf(a, b, k=60):
    s = {}
    for r, cid in enumerate(a): s[cid] = s.get(cid, 0) + 1/(k+r+1)
    for r, cid in enumerate(b): s[cid] = s.get(cid, 0) + 1/(k+r+1)
    return [c for c, _ in sorted(s.items(), key=lambda x: -x[1])]

def main():
    C, gold = load()
    ids = [c["chunk_id"] for c in C]
    texts = [c["text"] for c in C]
    queries = [qa["q"] for qa in QA]
    trap = [i for i, qa in enumerate(QA) if qa.get("trap")]
    cjk  = [i for i, qa in enumerate(QA) if qa.get("cjk")]
    tbl  = [i for i, qa in enumerate(QA)
            if any(C[j]["has_table"] for j in range(len(C)) if ids[j] in gold[i])]
    print(f"코퍼스 {len(C)}청크 / 질의 {len(QA)}개 (함정 {len(trap)}, 교차언어 {len(cjk)}, 표기반 {len(tbl)})\n")

    # ---- BM25 ----
    from rank_bm25 import BM25Okapi
    tok = lambda s: re.findall(r"[A-Za-z]+|\d+\.?\d*|[一-鿿]|[가-힣]+", s.lower())
    bm = BM25Okapi([tok(t) for t in texts])
    bm_ranked = []
    for q in queries:
        sc = bm.get_scores(tok(q))
        bm_ranked.append([ids[i] for i in sorted(range(len(ids)), key=lambda i: -sc[i])[:20]])
    rows = [("BM25 단독", "-", metrics(bm_ranked, gold), subset(bm_ranked, gold, trap),
             subset(bm_ranked, gold, cjk), subset(bm_ranked, gold, tbl), 0.0)]

    # ---- Dense ----
    from sentence_transformers import SentenceTransformer
    import torch
    dev = "cpu"   # 코퍼스 97청크 — CPU로 충분하며 MPS 종료 크래시를 회피
    for mid, label, qp, pp, trc in MODELS:
        t0 = time.time()
        try:
            m = SentenceTransformer(mid, device=dev, trust_remote_code=trc)
            E = m.encode([pp+t for t in texts], normalize_embeddings=True,
                         batch_size=8, show_progress_bar=False, convert_to_tensor=True)
            Q = m.encode([qp+q for q in queries], normalize_embeddings=True,
                         show_progress_bar=False, convert_to_tensor=True)
        except Exception as e:
            print(f"  [SKIP] {label}: {type(e).__name__}: {str(e)[:120]}", flush=True)
            continue
        sim = (Q @ E.T).cpu()
        dr = [[ids[i] for i in sim[r].argsort(descending=True)[:20].tolist()] for r in range(len(queries))]
        hy = [rrf(dr[r], bm_ranked[r]) for r in range(len(queries))]
        el = time.time()-t0
        rows.append((label, "dense", metrics(dr, gold), subset(dr, gold, trap),
                     subset(dr, gold, cjk), subset(dr, gold, tbl), el))
        rows.append((label, "hybrid", metrics(hy, gold), subset(hy, gold, trap),
                     subset(hy, gold, cjk), subset(hy, gold, tbl), el))
        print(f"  [OK] {label}: dense MRR {rows[-2][2]['mrr']:.3f} / hybrid MRR {rows[-1][2]['mrr']:.3f} ({el:.0f}s)", flush=True)
        del m, E, Q

    hdr = f"{'모델':18}{'방식':8}{'Hit@1':>7}{'Hit@3':>7}{'Hit@5':>7}{'MRR':>7}  {'함정MRR':>8}{'교차언어':>8}{'표MRR':>7}  {'초':>6}"
    print(hdr); print("-"*len(hdr)+"--")
    for label, mode, a, tr, cj, tb, el in rows:
        print(f"{label:18}{mode:8}{a['hit1']:7.2f}{a['hit3']:7.2f}{a['hit5']:7.2f}{a['mrr']:7.3f}"
              f"  {tr['mrr']:8.3f}{cj['mrr']:8.2f}{tb['mrr']:7.3f}  {el:6.1f}")
    json.dump([{"model":l,"mode":m,**a} for l,m,a,_,_,_,_ in rows],
              open(pathlib.Path(__file__).parent/"results.json","w"), indent=2, ensure_ascii=False)

if __name__ == "__main__":
    main()
