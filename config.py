"""Central config loaded from env. Single source of truth for paths and knobs."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Auto-enable HuggingFace offline mode if the configured embedding model
# is already cached locally. Avoids spurious DNS lookups / hangs when the
# network is flaky. User can override by setting HF_HUB_OFFLINE=0 in env.
def _maybe_force_hf_offline() -> None:
    if "HF_HUB_OFFLINE" in os.environ:
        return
    model_id = os.getenv("EMBED_MODEL", "BAAI/bge-small-en-v1.5")
    cache_dir = Path(os.getenv("HF_HOME", str(Path.home() / ".cache" / "huggingface"))) / "hub"
    model_dir = cache_dir / ("models--" + model_id.replace("/", "--"))
    if model_dir.exists():
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


_maybe_force_hf_offline()

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
TICKETS_DIR = REPO_ROOT / "support_tickets"
SAMPLE_CSV = TICKETS_DIR / "sample_support_tickets.csv"
INPUT_CSV = TICKETS_DIR / "support_tickets.csv"
OUTPUT_CSV = TICKETS_DIR / "output.csv"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Settings:
    provider: str = _env("LLM_PROVIDER", "groq")
    triage_model: str = _env("LLM_TRIAGE_MODEL", "llama-3.3-70b-versatile")
    respond_model: str = _env("LLM_RESPOND_MODEL", "llama-3.3-70b-versatile")
    judge_model: str = _env("LLM_JUDGE_MODEL", "llama-3.3-70b-versatile")
    embed_model: str = _env("EMBED_MODEL", "BAAI/bge-small-en-v1.5")
    top_k: int = _env_int("RETRIEVE_TOP_K", 8)
    min_score: float = _env_float("RETRIEVE_MIN_SCORE", 0.25)
    max_rpm: int = _env_int("LLM_MAX_RPM", 25)
    concurrency: int = _env_int("LLM_CONCURRENCY", 6)
    timeout_s: int = _env_int("LLM_TIMEOUT_S", 30)
    max_retries: int = _env_int("LLM_MAX_RETRIES", 2)
    cache_dir: Path = REPO_ROOT / _env("CACHE_DIR", "code/.cache")
    runs_dir: Path = REPO_ROOT / _env("RUNS_DIR", "code/.runs")


SETTINGS = Settings()
SETTINGS.cache_dir.mkdir(parents=True, exist_ok=True)
SETTINGS.runs_dir.mkdir(parents=True, exist_ok=True)

COMPANIES = ("HackerRank", "Claude", "Visa")
REQUEST_TYPES = ("product_issue", "feature_request", "bug", "invalid")
STATUSES = ("replied", "escalated")
