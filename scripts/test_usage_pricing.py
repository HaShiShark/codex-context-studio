from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.proxy_store import estimate_gpt56_sol_cost_usd  # noqa: E402


def test_gpt56_sol_reference_price() -> None:
    assert estimate_gpt56_sol_cost_usd(200_000, 0, 0) == 1.0
    assert estimate_gpt56_sol_cost_usd(200_000, 200_000, 0) == 0.1
    assert estimate_gpt56_sol_cost_usd(0, 0, 1_000_000) == 30.0
    assert estimate_gpt56_sol_cost_usd(272_000, 0, 100_000) == 4.36
    assert estimate_gpt56_sol_cost_usd(300_000, 100_000, 100_000) == 6.6


def main() -> None:
    test_gpt56_sol_reference_price()
    print("ok - 1 GPT-5.6 Sol usage pricing test passed")


if __name__ == "__main__":
    main()
