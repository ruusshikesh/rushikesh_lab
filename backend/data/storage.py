"""
Rush Algo — Persistent Storage
Strategies and deployments are saved to JSON files so they survive
server restarts. This directly fixes: 'strategy I created disappears
from the Backtest dropdown after restart'.
"""
from __future__ import annotations
import json
import logging
import os
import threading
from typing import Dict

from models.schemas import Deployment, Strategy

logger    = logging.getLogger(__name__)
DATA_DIR  = "data_store"
STRAT_FILE= os.path.join(DATA_DIR, "strategies.json")
DEPLOY_FILE = os.path.join(DATA_DIR, "deployments.json")

_lock = threading.Lock()


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_strategies() -> Dict[str, Strategy]:
    """Load saved strategies from disk. Returns {} if file doesn't exist yet."""
    _ensure_dir()
    if not os.path.exists(STRAT_FILE):
        return {}
    try:
        with _lock:
            with open(STRAT_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        out = {}
        for sid, data in raw.items():
            try:
                out[sid] = Strategy(**data)
            except Exception as exc:
                logger.warning("Skipping corrupt strategy %s: %s", sid, exc)
        logger.info("Loaded %d strategies from disk", len(out))
        return out
    except Exception as exc:
        logger.error("Failed to load strategies.json: %s — starting fresh", exc)
        return {}


def save_strategies(strategies: Dict[str, Strategy]) -> None:
    """Persist all strategies to disk. Called after every create/update/delete."""
    _ensure_dir()
    try:
        payload = {sid: s.model_dump(mode="json") for sid, s in strategies.items()}
        with _lock:
            tmp = STRAT_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
            os.replace(tmp, STRAT_FILE)   # atomic write — avoids corruption on crash
    except Exception as exc:
        logger.error("Failed to save strategies.json: %s", exc)


def load_deployments() -> Dict[str, Deployment]:
    """Load saved deployments from disk."""
    _ensure_dir()
    if not os.path.exists(DEPLOY_FILE):
        return {}
    try:
        with _lock:
            with open(DEPLOY_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        out = {}
        for did, data in raw.items():
            try:
                out[did] = Deployment(**data)
            except Exception as exc:
                logger.warning("Skipping corrupt deployment %s: %s", did, exc)
        logger.info("Loaded %d deployments from disk", len(out))
        return out
    except Exception as exc:
        logger.error("Failed to load deployments.json: %s — starting fresh", exc)
        return {}


def save_deployments(deployments: Dict[str, Deployment]) -> None:
    """Persist all deployments to disk."""
    _ensure_dir()
    try:
        payload = {did: d.model_dump(mode="json") for did, d in deployments.items()}
        with _lock:
            tmp = DEPLOY_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
            os.replace(tmp, DEPLOY_FILE)
    except Exception as exc:
        logger.error("Failed to save deployments.json: %s", exc)
