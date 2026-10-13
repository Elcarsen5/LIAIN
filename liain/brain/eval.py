#!/usr/bin/env python3
"""기억 인출 품질 평가 하네스 — recall@K + 회귀(negative) 측정.

왜: base-level·하이브리드 인출 재설계가 "얼마나 잘 떠오르나"를 수치로 감시/보정하는
토대. 골든셋(query→기대 기억) 대비 WorkingMemoryAssembler.retrieve_for_query를 돌려
recall@K와 무관 spillover 억제(negative)를 측정한다.

골든셋 스키마 (golden_set.json):
  {"cases": [
     {"id", "query", "speaking_with"?, "expect_any"?:[정규식/구절], "reject_any"?:[...], "note"}
  ]}
    - expect_any: top-K 라인에 하나라도 뜨면 hit (positive recall)
    - reject_any: top-K 라인에 하나도 안 떠야 pass (negative, spillover 억제)

데이터·config 위치는 환경변수로(import 시점에 읽힘):
  LIAIN_DATA_DIR   — brain/records.json·episodes·short_term 위치 (기본 cwd)
  LIAIN_CONFIG_DIR — contacts.yaml·persona.yaml 위치 (별칭/표시명 해석용)

사용(예시 세트):
  LIAIN_CONFIG_DIR=examples/eval/config LIAIN_DATA_DIR=examples/eval \\
    python -m liain.brain.eval --set examples/eval/golden_set.json --k 8
"""
import argparse
import json
import os
import re
import sys


def _build_wm():
    """ingest 후 retriever 주입한 WorkingMemoryAssembler 반환."""
    from .working import WorkingMemoryAssembler
    from .retrieval import get_default_retriever
    from .manager import Brain
    retr = get_default_retriever()
    # FTS 인덱스는 마지막 ingest 스냅샷 — 평가 전 최신화(멱등).
    try:
        stats = retr.ingest_all()
        print(f"  ingest: {stats.get('inserted_or_updated', 0)}건 "
              f"{stats.get('by_source', {})}", flush=True)
    except Exception as e:
        print(f"  ingest 실패(계속): {e}", flush=True)
    return WorkingMemoryAssembler(Brain(), retriever=retr)


def _hit(text, phrases):
    """phrases 중 하나라도 text에 (정규식) 매치되면 True."""
    for p in phrases or []:
        try:
            if re.search(p, text):
                return True
        except re.error:
            if p in text:
                return True
    return False


def run(golden_path, k):
    cases = json.load(open(golden_path))["cases"]
    wm = _build_wm()
    pos_total = pos_hit = 0
    neg_total = neg_pass = 0
    rows = []
    for c in cases:
        lines = wm.retrieve_for_query(
            c["query"], top_k=k, speaking_with=c.get("speaking_with"))
        text = "\n".join(lines)
        res = {"id": c["id"], "note": c.get("note", ""), "n_lines": len(lines)}
        if c.get("expect_any"):
            hit = _hit(text, c["expect_any"])
            pos_total += 1
            pos_hit += int(hit)
            res["kind"] = "recall"
            res["pass"] = hit
        if c.get("reject_any"):
            clean = not _hit(text, c["reject_any"])
            neg_total += 1
            neg_pass += int(clean)
            res["kind"] = "negative"
            res["pass"] = clean
        rows.append(res)
    return rows, (pos_hit, pos_total), (neg_pass, neg_total)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--set", default=os.path.join(
        os.environ.get("LIAIN_DATA_DIR", os.getcwd()), "golden_set.json"))
    args = ap.parse_args()

    if not os.path.exists(args.set):
        print(f"골든셋 없음: {args.set}\n  --set 으로 지정하거나 LIAIN_DATA_DIR를 설정해.",
              file=sys.stderr)
        sys.exit(1)

    rows, (ph, pt), (ng, nt) = run(args.set, args.k)
    print(f"  골든셋: {os.path.basename(args.set)}  (K={args.k})\n")
    for r in rows:
        mark = "✅" if r.get("pass") else "❌"
        print(f"  {mark} [{r.get('kind','?'):8}] {r['id']:20} lines={r['n_lines']}  {r['note']}")
    print()
    if pt:
        print(f"  recall@{args.k} (positive): {ph}/{pt} = {ph/pt:.0%}")
    if nt:
        print(f"  negative 억제: {ng}/{nt} = {ng/nt:.0%}")


if __name__ == "__main__":
    main()
