# -*- coding: utf-8 -*-
"""로더 A/B: 동일 임베딩(bge-m3)·동일 골든 QA로 로더만 바꿔 검색 성능 비교."""
import json, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from golden_qa import QA, match

HERE = pathlib.Path(__file__).parent
LOADERS = ["pymupdf4llm", "pypdf", "pdfplumber"]
MODEL = "BAAI/bge-m3"


def metrics(ranked, gold):
    h1 = h3 = h5 = 0; mrr = 0.0; unreach = 0
    for r, g in zip(ranked, gold):
        if not g: unreach += 1; continue          # 정답이 코퍼스에 아예 없음
        pos = next((i for i, cid in enumerate(r) if cid in g), None)
        if pos is None: continue
        if pos < 1: h1 += 1
        if pos < 3: h3 += 1
        if pos < 5: h5 += 1
        mrr += 1 / (pos + 1)
    n = len(gold)
    return dict(hit1=h1/n, hit3=h3/n, hit5=h5/n, mrr=mrr/n, unreachable=unreach)


def sub(ranked, gold, idx):
    return metrics([ranked[i] for i in idx], [gold[i] for i in idx])


def main():
    from sentence_transformers import SentenceTransformer
    m = SentenceTransformer(MODEL, device="cpu")
    queries = [qa["q"] for qa in QA]
    Q = m.encode(queries, normalize_embeddings=True, show_progress_bar=False, convert_to_tensor=True)
    trap = [i for i, qa in enumerate(QA) if qa.get("trap")]
    cjk  = [i for i, qa in enumerate(QA) if qa.get("cjk")]

    rows = []
    for L in LOADERS:
        C = [json.loads(l) for l in open(HERE / f"chunks_{L}.jsonl")]
        ids = [c["chunk_id"] for c in C]
        gold = [{c["chunk_id"] for c in C if c["tech_id"] == qa["tech"] and match(c["text"], qa["gold"])}
                for qa in QA]
        t0 = time.time()
        E = m.encode([c["text"] for c in C], normalize_embeddings=True,
                     batch_size=8, show_progress_bar=False, convert_to_tensor=True)
        sim = (Q @ E.T).cpu()
        ranked = [[ids[i] for i in sim[r].argsort(descending=True)[:20].tolist()] for r in range(len(QA))]
        a = metrics(ranked, gold)
        tbl = [i for i in range(len(QA)) if any(c["has_table"] for c in C if c["chunk_id"] in gold[i])]
        rows.append((L, len(C), a, sub(ranked, gold, trap), sub(ranked, gold, cjk),
                     sub(ranked, gold, tbl) if tbl else None, time.time() - t0))
        print(f"  [OK] {L}: MRR {a['mrr']:.3f} (도달불가 {a['unreachable']}건)", flush=True)

    print(f"\n{'로더':14}{'청크':>5}{'Hit@1':>7}{'Hit@3':>7}{'Hit@5':>7}{'MRR':>8}{'함정MRR':>9}{'교차언어':>9}{'표MRR':>8}{'도달불가':>8}")
    print("-" * 92)
    for L, n, a, tr, cj, tb, el in rows:
        tbm = f"{tb['mrr']:.3f}" if tb else "  -  "
        print(f"{L:14}{n:5}{a['hit1']:7.2f}{a['hit3']:7.2f}{a['hit5']:7.2f}{a['mrr']:8.3f}"
              f"{tr['mrr']:9.3f}{cj['mrr']:9.2f}{tbm:>8}{a['unreachable']:8}")
    json.dump([{"loader": L, "chunks": n, **a} for L, n, a, _, _, _, _ in rows],
              open(HERE / "results_loader.json", "w"), indent=2, ensure_ascii=False)

if __name__ == "__main__":
    main()
