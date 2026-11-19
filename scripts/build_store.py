"""2단계: 1단계 파싱 결과로 API 가 읽는 표(build/store/*.parquet)를 만들고 불변조건을 검사한다.

실행: PYTHONPATH=src python scripts/build_store.py   (먼저 scripts/run_phase1.py 를 돌려 둘 것)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd

from prelim.store import build_store, validate

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
OUT = BUILD / "store"


def main() -> int:
    t0 = time.time()
    filings, metrics = build_store(pd.read_parquet(BUILD / "parsed_filings.parquet"),
                                   pd.read_parquet(BUILD / "parsed_metrics.parquet"))
    OUT.mkdir(parents=True, exist_ok=True)
    filings.to_parquet(OUT / "filings.parquet", index=False)
    metrics.to_parquet(OUT / "metrics.parquet", index=False)

    violations = validate(filings, metrics)
    chains = filings.groupby("chain_id")
    summary = {
        "filings": int(len(filings)),
        "chains": int(chains.ngroups),
        "chains_with_corrections": int((chains.size() > 1).sum()),
        "corrections_without_document": int((~filings["document_parsed"]).sum()),
        "corrections_not_linked": int((~filings["document_parsed"] & (filings["versions_in_chain"] == 1)).sum()),
        "chains_latest_not_parsed": int((~filings[filings["is_latest"]]["chain_latest_parsed"]).sum()),
        "time_match": filings["time_match"].value_counts().to_dict(),
        "time_category": filings["time_cat"].value_counts().to_dict(),
        "period_key_unknown": int((filings["period_key"] == "UNKNOWN").sum()),
        "with_consensus": int(filings["consensus_krw"].notna().sum()),
        "surprise_label": filings["surprise_label"].value_counts().to_dict(),
        "metric_rows": int(len(metrics)),
        "invariant_violations": violations,
        "elapsed_sec": round(time.time() - t0),
    }
    (ROOT / "reports" / "phase2_store_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))
    return 0 if all(v == 0 for v in violations.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
