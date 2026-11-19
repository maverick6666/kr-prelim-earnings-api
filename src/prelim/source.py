"""DART 잠정실적 원문 읽기.

공시 목록(inventory)과 원문 ZIP 을 접수번호(rcept_no)로 잇고, 원문 인코딩을 판별한다.
원문은 2008년 무렵부터 같은 표 양식을 쓰지만, 인코딩은 해마다 UTF-8 과 CP949(EUC-KR)가 섞여 있다.
데이터 위치는 환경변수 PRELIM_DATA_ROOT 로 바꿀 수 있다(기본: 개발 PC 의 데이터 폴더).
"""

from __future__ import annotations

import os
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

DATA_ROOT = Path(os.environ.get("PRELIM_DATA_ROOT", r"F:\quant\data"))
INVENTORY_PATH = Path(
    "derived/kr_prelim_earnings/kr_dart_preliminary_earnings_inventory_v1_20260719/"
    "preliminary_earnings_inventory.parquet"
)
DOC_DIR = Path("raw/dart_docs/prelim_earnings/cache/c")


@dataclass(frozen=True)
class RawDocument:
    rcept_no: str
    html: str
    encoding: str  # "utf-8" | "cp949" | "cp949-lossy"


def doc_path(rcept_no: str, root: Path = DATA_ROOT) -> Path:
    return root / DOC_DIR / rcept_no[:2] / rcept_no / "document.zip"


def load_inventory(root: Path = DATA_ROOT) -> pd.DataFrame:
    """잠정실적 공시 목록. 월별 실적 공시는 분기 실적과 성격이 달라 표시만 해 둔다."""
    inv = pd.read_parquet(root / INVENTORY_PATH)
    inv["has_document"] = [doc_path(r, root).exists() for r in inv["rcept_no"]]
    return inv


def decode(payload: bytes) -> tuple[str, str]:
    """UTF-8 을 먼저 시도하고, 실패하면 CP949 로 읽는다. 둘 다 안 되면 깨진 글자를 치환해 읽는다."""
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return payload.decode(encoding), "utf-8" if encoding == "utf-8-sig" else encoding
        except UnicodeDecodeError:
            continue
    return payload.decode("cp949", errors="replace"), "cp949-lossy"


def read_document(rcept_no: str, root: Path = DATA_ROOT) -> RawDocument | None:
    path = doc_path(rcept_no, root)
    if not path.exists():
        return None
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith((".xml", ".htm", ".html"))]
        payload = archive.read((names or archive.namelist())[0])
    html, encoding = decode(payload)
    return RawDocument(rcept_no=rcept_no, html=html, encoding=encoding)
