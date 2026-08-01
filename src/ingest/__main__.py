"""Entry point for `python -m src.ingest`: run one full RSS poll cycle."""

import logging

from src.ingest.pollers import poll_all

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

if __name__ == "__main__":
    poll_all()
