#!/usr/bin/env python
"""Initialize a market-data-db data lake + metadata database at a given path.

Usage: python scripts/init_db.py [--root ./data] [--metadata-url sqlite:///...]
"""
from __future__ import annotations

import argparse

from market_data_db import DBConfig, MarketDataDB


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="./data")
    parser.add_argument("--metadata-url", default=None)
    args = parser.parse_args()

    config = DBConfig(root=args.root, metadata_url=args.metadata_url)
    db = MarketDataDB(config)
    print(f"Initialized data lake at {config.root}")
    print(f"  landing:   {config.landing_root}")
    print(f"  canonical: {config.canonical_root}")
    print(f"  archive:   {config.root / 'archive'}")
    print(f"  metadata:  {config.metadata_url}")
    db.close()


if __name__ == "__main__":
    main()
