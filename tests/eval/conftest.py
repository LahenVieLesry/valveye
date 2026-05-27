from __future__ import annotations

import json
from pathlib import Path

import pytest

BENCHMARK_PATH = Path(__file__).parent / "benchmark.json"


def load_benchmark() -> list[dict]:
    return json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))


@pytest.fixture(params=load_benchmark(), ids=lambda c: c["id"])
def benchmark_case(request) -> dict:
    return request.param
