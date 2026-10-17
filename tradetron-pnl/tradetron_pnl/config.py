"""Settings (environment / .env) and the per-deployment tag table.

Secrets never live in files that are committed: the API key and the Telegram
credentials come from environment variables (GitHub Actions secrets in CI, a
local ``.env`` for development — ``.env`` is git-ignored).
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Optional

DEFAULT_BASE_URL = "https://tradetron.tech/api/v1"
DEFAULT_DEPLOYMENTS_PATH = "/me/deployments"
DEFAULT_POSITIONS_PATH = "/me/deployments/{id}/positions"
DEFAULT_NOTIFICATIONS_PATH = "/me/notifications"

PNL_SEMANTICS = ("cumulative", "today")

# How a deployment's P&L is taxed in India depends on what it trades, and the
# Developer API does not say. Tag deployments in config/deployments.csv.
SEGMENTS = (
    "fno",              # equity / index futures & options  -> non-speculative business
    "currency",         # currency derivatives               -> non-speculative business
    "commodity",        # MCX / NCDEX derivatives            -> non-speculative business
    "intraday_equity",  # cash-segment intraday              -> speculative business
    "delivery_equity",  # cash-segment delivery              -> capital gains
    "crypto",           # Delta Exchange etc.                -> VDA (115BBH)
    "other",
)

TRUE_WORDS = {"1", "true", "yes", "y", "on"}
FALSE_WORDS = {"0", "false", "no", "n", "off", ""}


def truthy(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in TRUE_WORDS:
        return True
    if text in FALSE_WORDS:
        return False
    raise ValueError(f"expected a boolean, got {value!r}")


@dataclass
class Settings:
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    deployments_path: str = DEFAULT_DEPLOYMENTS_PATH
    positions_path: str = DEFAULT_POSITIONS_PATH
    notifications_path: str = DEFAULT_NOTIFICATIONS_PATH
    telegram_token: str = ""
    telegram_chat_id: str = ""
    pnl_semantics: str = "cumulative"
    default_segment: str = "fno"
    reporting_currency: str = "INR"
    fetch_positions: bool = True
    max_positions: int = 25
    preopen_message: bool = False
    silent_hourly: bool = False  # deliver hourly updates without a notification sound
    timeout: float = 20.0
    pace: float = 0.25           # seconds between per-deployment position calls

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    def validate(self) -> None:
        if self.pnl_semantics not in PNL_SEMANTICS:
            raise ValueError(
                f"TTPNL_PNL_SEMANTICS must be one of {PNL_SEMANTICS}, got {self.pnl_semantics!r}"
            )
        if self.default_segment not in SEGMENTS:
            raise ValueError(
                f"TTPNL_DEFAULT_SEGMENT must be one of {SEGMENTS}, got {self.default_segment!r}"
            )
        if self.max_positions < 0:
            raise ValueError("TTPNL_MAX_POSITIONS must be >= 0")
        if "{id}" not in self.positions_path:
            raise ValueError("TRADETRON_POSITIONS_PATH must contain the {id} placeholder")


def load_dotenv(path: Path) -> Dict[str, str]:
    """Parse ``KEY=VALUE`` lines. Does not touch ``os.environ``; the caller merges."""
    values: Dict[str, str] = {}
    path = Path(path)
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def settings_from_env(env: Mapping[str, str] = os.environ, dotenv: Optional[Path] = None) -> Settings:
    merged: Dict[str, str] = {}
    if dotenv is not None:
        merged.update(load_dotenv(dotenv))
    merged.update({k: v for k, v in env.items()})   # real environment wins

    def get(name: str, default: str = "") -> str:
        return merged.get(name, default)

    settings = Settings(
        api_key=get("TRADETRON_API_KEY").strip(),
        base_url=(get("TRADETRON_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
        deployments_path=get("TRADETRON_DEPLOYMENTS_PATH") or DEFAULT_DEPLOYMENTS_PATH,
        positions_path=get("TRADETRON_POSITIONS_PATH") or DEFAULT_POSITIONS_PATH,
        notifications_path=get("TRADETRON_NOTIFICATIONS_PATH") or DEFAULT_NOTIFICATIONS_PATH,
        telegram_token=get("TELEGRAM_BOT_TOKEN").strip(),
        telegram_chat_id=get("TELEGRAM_CHAT_ID").strip(),
        pnl_semantics=(get("TTPNL_PNL_SEMANTICS") or "cumulative").strip().lower(),
        default_segment=(get("TTPNL_DEFAULT_SEGMENT") or "fno").strip().lower(),
        reporting_currency=(get("TTPNL_CURRENCY") or "INR").strip().upper(),
        fetch_positions=truthy(get("TTPNL_FETCH_POSITIONS", "1"), True),
        max_positions=int(get("TTPNL_MAX_POSITIONS") or 25),
        preopen_message=truthy(get("TTPNL_PREOPEN_MESSAGE", "0"), False),
        silent_hourly=truthy(get("TTPNL_SILENT_HOURLY", "0"), False),
        timeout=float(get("TTPNL_TIMEOUT") or 20.0),
        pace=float(get("TTPNL_PACE") or 0.25),
    )
    settings.validate()
    return settings


# --------------------------------------------------------------------------- #
# Deployment tags
# --------------------------------------------------------------------------- #
@dataclass
class DeploymentTag:
    deployment_id: Optional[int]
    alias: str = ""
    segment: str = "fno"
    include: bool = True
    notes: str = ""
    configured: bool = field(default=False, repr=False)


TAG_COLUMNS = ["deployment_id", "alias", "segment", "include", "notes"]


def load_deployment_tags(path: Path) -> Dict[int, DeploymentTag]:
    """Read ``config/deployments.csv``. Missing file -> empty table (all defaults)."""
    tags: Dict[int, DeploymentTag] = {}
    path = Path(path)
    if not path.exists():
        return tags
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(_strip_comments(fh)):
            raw_id = (row.get("deployment_id") or "").strip()
            if not raw_id:
                continue
            try:
                dep_id = int(raw_id)
            except ValueError:
                raise ValueError(f"{path}: deployment_id must be an integer, got {raw_id!r}") from None
            segment = (row.get("segment") or "fno").strip().lower() or "fno"
            if segment not in SEGMENTS:
                raise ValueError(f"{path}: unknown segment {segment!r} for {dep_id}; choose from {SEGMENTS}")
            tags[dep_id] = DeploymentTag(
                deployment_id=dep_id,
                alias=(row.get("alias") or "").strip(),
                segment=segment,
                include=truthy((row.get("include") or "1").strip(), True),
                notes=(row.get("notes") or "").strip(),
                configured=True,
            )
    return tags


def _strip_comments(lines):
    for line in lines:
        if line.lstrip().startswith("#"):
            continue
        yield line


def tag_for(dep_id: int, tags: Mapping[int, DeploymentTag], default_segment: str = "fno") -> DeploymentTag:
    tag = tags.get(dep_id)
    if tag is not None:
        return tag
    return DeploymentTag(deployment_id=dep_id, segment=default_segment)
