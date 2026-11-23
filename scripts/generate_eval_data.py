"""Regenerate the versioned SYNTHETIC regression fixture without model APIs."""

from __future__ import annotations

import argparse
from pathlib import Path

from semantic_eval.dataset import DEFAULT_SEED, NOTICE, generate_dataset, write_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--output", type=Path, default=Path("data/synthetic-v1.json"))
    args = parser.parse_args()
    dataset = generate_dataset(args.seed)
    write_dataset(dataset, args.output)
    print(NOTICE)
    print(f"Generated {len(dataset.tools)} tools and {len(dataset.queries)} queries.")


if __name__ == "__main__":
    main()
