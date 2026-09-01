#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyyaml",
# ]
# ///

"""
LiteLLM / Zoo Code dynamic configuration generator.

Categories:
1. popularity   : Top token-consumption models (weekly usage), discounted prioritized.
2. practical    : High-capability & intelligent models (benchmark-backed), discounted prioritized.
3. great_deal   : Currently discounted (special price) models, sorted by lowest price & popularity.

Each category registers:
- A primary group entry ('popularity', 'practical', 'great_deal') with automatic fallback to #2 and #3.
- Individual direct entries ('popularity-<model>', 'practical-<model>', 'great_deal-<model>').
"""

from __future__ import annotations

import concurrent.futures
import copy
import json
import logging
import math
import os
import stat
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # pyright: ignore[reportMissingImports]

logger = logging.getLogger(__name__)


# =============================================================================
# Paths & URLs
# =============================================================================

BASE_YAML_PATH = Path("base_llm.yaml")
OUTPUT_YAML_PATH = Path("config.yaml")
ENV_FILE_PATH = Path(".env")

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_RANKINGS_URL = (
    "https://openrouter.ai/api/v1/datasets/rankings-daily?period=week"
)
OPENROUTER_DISCOUNT_URL = (
    "https://openrouter.ai/api/frontend/v1/models/find"
    "?active=true&discount=true&fmt=cards"
)
MODELGREP_API_URL = "https://modelgrep.com/api/v1/models"

OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
DISCORD_WEBHOOK_ENV = "DISCORD_WEBHOOK_URL"
MODELGREP_COMPARE_ENABLED_ENV = "MODELGREP_COMPARE_ENABLED"
MODELGREP_COMPARE_LIMIT_ENV = "MODELGREP_COMPARE_LIMIT"
MODELGREP_COMPARE_TOP_ENV = "MODELGREP_COMPARE_TOP"

# Candidates per category
TOP_N = 3

# Minimum practical context length (tokens)
MIN_CONTEXT_LENGTH = 32_768

# Low-price ceiling proxy for Great Deal fallback (USD per 1M tokens)
BARGAIN_PRICE_INPUT_CEILING = 0.25

# High capability threshold for Practical category
PRACTICAL_MIN_INTELLIGENCE = 50.0

# [S5] Maximum bytes to read from a single API response (50 MB).
# Guards against memory exhaustion from maliciously large payloads.
_MAX_RESPONSE_BYTES = 50 * 1024 * 1024

# [S1] Allowed Discord Webhook URL prefixes (SSRF prevention).
_DISCORD_WEBHOOK_PREFIXES = (
    "https://discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
)

# File permission for generated config files (owner read/write only).
_CONFIG_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR  # 0o600


# =============================================================================
# Filter Settings
# =============================================================================

TRUSTED_PROVIDERS = (
    "openai/",
    "deepseek/",
    "qwen/",
    "z-ai/",
    "minimax/",
    "xiaomi/",
    "inclusionai/",
    "tencent/",
    "meta-llama/",
    "mistralai/",
    "anthropic/",
    "google/",
)

EXCLUDED_SUFFIXES = (":batch",)

EXCLUDED_KEYWORDS = (
    "embedding",
    "rerank",
    "moderation",
    "transcription",
    "tts",
    "audio-preview",
    "-audio",
    "-image",
    "-image-preview",
)

LOW_TIER_KEYWORDS = (
    "nano",
    "small",
    "lite",
    "-1b",
    ":1b",
    "-3b",
    ":3b",
    "mini",
)


# =============================================================================
# Category Definitions
# =============================================================================

@dataclass(frozen=True)
class CategoryConfig:
    name: str
    display_title: str
    description: str
    max_candidates: int = TOP_N


CATEGORIES = {
    "popularity": CategoryConfig(
        name="popularity",
        display_title="🔥 Popularity (総合人気枠)",
        description="週間トークン消費量上位の人気モデル（特別割引優先）",
    ),
    "practical": CategoryConfig(
        name="practical",
        display_title="⚡ Practical (高機能・実用枠)",
        description="知性ベンチマーク50+の実用的・高機能モデル（特別割引優先）",
    ),
    "great_deal": CategoryConfig(
        name="great_deal",
        display_title="🏷️ Great Deal (格安・特別割引枠)",
        description="特別割引中の格安・高コスパモデル（最安＆人気順）",
    ),
}

PUBLIC_GROUP_NAMES = frozenset(CATEGORIES.keys())

# [E5] Pre-built tuple of dynamic prefixes for efficient startswith() checks.
_DYNAMIC_PREFIXES = tuple(f"{g}-" for g in PUBLIC_GROUP_NAMES)


# =============================================================================
# Logging
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# =============================================================================
# Environment Loader
# =============================================================================

def load_env_file() -> None:
    """Minimal .env loader. Existing environment variables always win."""
    if not ENV_FILE_PATH.exists():
        return

    with ENV_FILE_PATH.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()

            # [S4] Only strip matching quote pairs (both ends same char).
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]

            if key and key not in os.environ:
                os.environ[key] = value


# =============================================================================
# Safe HTTP Read
# =============================================================================

def _safe_read(response: Any, max_bytes: int = _MAX_RESPONSE_BYTES) -> bytes:
    """
    [S5] Read up to max_bytes from an HTTP response.
    Raises ValueError if the response exceeds the limit.
    """
    data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(
            f"API response exceeded size limit ({max_bytes:,} bytes)"
        )
    return data


# =============================================================================
# Generic Helpers
# =============================================================================

def parse_price(value: Any) -> float:
    """OpenRouter exposes price per token. Return USD per 1M tokens."""
    try:
        price = float(value or 0.0)
    except (TypeError, ValueError):
        return float("inf")

    if not math.isfinite(price) or price < 0.0:
        return float("inf")

    return price * 1_000_000


def get_context_length(model: dict[str, Any]) -> int:
    try:
        return int(model.get("context_length") or 0)
    except (TypeError, ValueError):
        return 0


def get_created(model: dict[str, Any]) -> int:
    try:
        return int(model.get("created") or 0)
    except (TypeError, ValueError):
        return 0


def sale_price(model: dict[str, Any]) -> tuple[float, float]:
    pricing = model.get("pricing") or {}
    if not isinstance(pricing, dict):
        return float("inf"), float("inf")
    return parse_price(pricing.get("prompt")), parse_price(pricing.get("completion"))


def total_price(model: dict[str, Any]) -> float:
    inp, out = sale_price(model)
    return inp + out


def is_always_free_model(model: dict[str, Any]) -> bool:
    model_id = str(model.get("id") or "").lower()
    input_cost, output_cost = sale_price(model)
    return model_id.endswith(":free") or (input_cost == 0.0 and output_cost == 0.0)


def model_id_to_name(model_id: str, prefix: str = "") -> str:
    """
    openai/gpt-4o-mini -> prefix + 'openai-gpt-4o-mini'
    """
    clean = model_id.replace("/", "-").replace(":", "-")
    if prefix:
        return f"{prefix}-{clean}"
    return f"openrouter-{clean}"


def fixed_openrouter_model_ids(base_config: dict[str, Any]) -> set[str]:
    fixed: set[str] = set()
    model_list = base_config.get("model_list", [])
    if not isinstance(model_list, list):
        return fixed
    for entry in model_list:
        if not isinstance(entry, dict):
            continue
        params = entry.get("litellm_params")
        if not isinstance(params, dict):
            continue
        model_ref = str(params.get("model") or "")
        if model_ref.startswith("openrouter/"):
            fixed.add(model_ref.removeprefix("openrouter/"))
    return fixed


# =============================================================================
# [E3] Pre-computed model text cache
# =============================================================================

def _cache_model_text(model: dict[str, Any]) -> None:
    """
    Pre-compute and cache the lower-case concatenated text fields used by
    multiple classification functions (is_coder_model, is_reasoning_model, etc).
    Called once per model during the preprocessing phase.
    """
    if "_text_lower" not in model:
        model["_text_lower"] = " ".join(
            str(model.get(k) or "").lower() for k in ("id", "name", "description")
        )


# =============================================================================
# ModelGrep Helpers
# =============================================================================

def mg_model(model: dict[str, Any]) -> dict[str, Any] | None:
    mg = model.get("_modelgrep")
    return mg if isinstance(mg, dict) else None


def mg_aa(model: dict[str, Any], key: str) -> float | None:
    """Read an Artificial Analysis benchmark score from ModelGrep."""
    mg = mg_model(model)
    if mg is None:
        return None
    benchmarks = mg.get("benchmarks") or {}
    if not isinstance(benchmarks, dict):
        return None
    artificial = benchmarks.get("artificial_analysis") or {}
    if not isinstance(artificial, dict):
        return None
    value = artificial.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def mg_cap(model: dict[str, Any], key: str) -> bool | None:
    mg = mg_model(model)
    if mg is None:
        return None
    capabilities = mg.get("capabilities") or {}
    if not isinstance(capabilities, dict):
        return None
    value = capabilities.get(key)
    return bool(value) if isinstance(value, bool) else None


def mg_pricing(model: dict[str, Any]) -> tuple[float, float] | None:
    mg = mg_model(model)
    if mg is None:
        return None
    pricing = mg.get("pricing") or {}
    if not isinstance(pricing, dict):
        return None
    try:
        input_cost = float(pricing.get("input") or 0.0)
        output_cost = float(pricing.get("output") or 0.0)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(input_cost) and math.isfinite(output_cost)):
        return None
    return input_cost, output_cost


def is_vision_model(model: dict[str, Any]) -> bool:
    vision = mg_cap(model, "vision")
    if vision is not None:
        return vision

    architecture = model.get("architecture") or {}
    if not isinstance(architecture, dict):
        architecture = {}

    modality = str(architecture.get("modality") or "").lower()
    model_id = str(model.get("id") or "").lower()
    pricing = model.get("pricing") or {}
    if not isinstance(pricing, dict):
        pricing = {}

    try:
        image_price = float(pricing.get("image") or 0.0)
    except (TypeError, ValueError):
        image_price = 0.0

    return "image" in modality or "vl" in model_id or image_price > 0


def is_coder_model(model: dict[str, Any]) -> bool:
    coding = mg_cap(model, "tools")
    if coding is True:
        return True

    # [E3] Use pre-cached text field.
    text = model.get("_text_lower") or ""
    return any(
        token in text
        for token in (
            "coder",
            "coding",
            "codex",
            "code",
            "developer",
            "programming",
            "dev",
        )
    )


def is_reasoning_model(model: dict[str, Any]) -> bool:
    reasoning = mg_cap(model, "reasoning")
    if reasoning is True:
        return True

    # [E3] Use pre-cached text field.
    text = model.get("_text_lower") or ""
    return any(
        token in text
        for token in (
            "reasoning",
            "reasoner",
            "thinking",
            "think",
            "-r1",
            "/r1",
            " r1",
            "o1",
            "o3",
            "pro",
            "opus",
            "sonnet",
        )
    )


def is_high_capability_model(model: dict[str, Any]) -> bool:
    """
    Check if a model qualifies as 'high capability' for the Practical category.
    """
    intelligence = mg_aa(model, "intelligence")
    if intelligence is not None:
        return intelligence >= PRACTICAL_MIN_INTELLIGENCE

    # Fallback when ModelGrep intelligence benchmark is not available.
    # [E3] Use pre-cached text field.
    text = model.get("_text_lower") or ""
    if any(k in text for k in LOW_TIER_KEYWORDS):
        return False

    return is_reasoning_model(model) or is_coder_model(model)


# =============================================================================
# Model Filtering & Eligibility
# =============================================================================

def is_interactive_model(model: dict[str, Any]) -> bool:
    """Filter out non-chat / non-interactive models."""
    model_id = str(model.get("id") or "").strip()
    if not model_id or not model_id.startswith(TRUSTED_PROVIDERS):
        return False

    lower_id = model_id.lower()
    if any(lower_id.endswith(suffix) for suffix in EXCLUDED_SUFFIXES):
        return False

    if any(keyword in lower_id for keyword in EXCLUDED_KEYWORDS):
        return False

    return get_context_length(model) >= MIN_CONTEXT_LENGTH


# =============================================================================
# Data Fetchers (with [S5] size-limited reads)
# =============================================================================

def fetch_openrouter_models() -> list[dict[str, Any]] | None:
    request = urllib.request.Request(
        OPENROUTER_API_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": "LiteLLM-Updater/5.1",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(_safe_read(response).decode("utf-8"))

        if not isinstance(payload, dict):
            raise TypeError("OpenRouter API response must be a JSON object")

        models = payload.get("data")
        if not isinstance(models, list) or not all(
            isinstance(m, dict) for m in models
        ):
            raise TypeError("OpenRouter API returned invalid model list")

        return models

    except (
        OSError,
        TimeoutError,
        UnicodeError,
        ValueError,
        TypeError,
        urllib.error.URLError,
    ) as exc:
        logger.error("OpenRouter API取得失敗: %s", exc)
        return None


def fetch_openrouter_discounted_models() -> set[str]:
    """Fetch set of model slugs currently discounted on OpenRouter."""
    request = urllib.request.Request(
        OPENROUTER_DISCOUNT_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": "LiteLLM-Updater/5.1",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(_safe_read(response).decode("utf-8"))

        if not isinstance(payload, dict):
            return set()

        data = payload.get("data")
        if not isinstance(data, dict):
            return set()

        models = data.get("models")
        if not isinstance(models, list):
            return set()

        slugs: set[str] = set()
        for model in models:
            if isinstance(model, dict):
                slug = str(model.get("slug") or "").strip()
                if slug:
                    slugs.add(slug)

        logger.info("OpenRouter 特別割引モデル取得件数: %d", len(slugs))
        return slugs

    except (
        OSError,
        TimeoutError,
        UnicodeError,
        ValueError,
        TypeError,
        urllib.error.URLError,
    ) as exc:
        logger.warning("OpenRouter 割引API取得失敗（スキップ）: %s", exc)
        return set()


def fetch_openrouter_usage_rankings() -> dict[str, int]:
    """Fetch official OpenRouter weekly token-consumption rankings."""
    api_key = os.environ.get(OPENROUTER_API_KEY_ENV, "").strip()
    if not api_key:
        logger.warning(
            "OPENROUTER_API_KEY is not configured; popularity ranking skipped"
        )
        return {}

    request = urllib.request.Request(
        OPENROUTER_RANKINGS_URL,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "LiteLLM-Updater/5.1",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(_safe_read(response).decode("utf-8"))
    except (
        OSError,
        TimeoutError,
        UnicodeError,
        ValueError,
        urllib.error.HTTPError,
        urllib.error.URLError,
    ) as exc:
        logger.warning("OpenRouter rankings API取得失敗: %s", exc)
        return {}

    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return {}

    token_totals: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("model_permaslug") or "").strip()
        if not model_id or model_id == "other":
            continue
        try:
            tokens = int(entry.get("total_tokens") or 0)
        except (TypeError, ValueError):
            continue
        if tokens > 0:
            token_totals[model_id] = token_totals.get(model_id, 0) + tokens

    ranked = sorted(token_totals.items(), key=lambda item: (-item[1], item[0]))
    logger.info("OpenRouter 週間利用ランキング取得件数: %d", len(ranked))
    return {model_id: rank for rank, (model_id, _) in enumerate(ranked, start=1)}


def popularity_rank(
    model: dict[str, Any],
    popularity_ranks: dict[str, int],
) -> int:
    identifiers = (
        str(model.get("id") or "").strip(),
        str(model.get("canonical_slug") or "").strip(),
        str(model.get("permaslug") or "").strip(),
    )
    matched_ranks = [
        popularity_ranks[identifier]
        for identifier in identifiers
        if identifier in popularity_ranks
    ]
    return min(matched_ranks, default=sys.maxsize)


def fetch_modelgrep_models() -> list[dict[str, Any]] | None:
    limit = 200
    models: list[dict[str, Any]] = []
    offset = 0

    try:
        while True:
            url = f"{MODELGREP_API_URL}?limit={limit}&offset={offset}"
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "LiteLLM-ModelGrep-Fetcher/1.0",
                },
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(_safe_read(response).decode("utf-8"))

            page = payload.get("data") if isinstance(payload, dict) else None
            meta = payload.get("meta") if isinstance(payload, dict) else {}
            if not isinstance(page, list) or not all(
                isinstance(item, dict) for item in page
            ):
                break
            models.extend(page)

            if not isinstance(meta, dict) or not meta.get("has_more") or not page:
                break
            offset += len(page)
            if offset > 10_000:
                break
    except Exception as exc:
        logger.warning("ModelGrep カタログ取得失敗（OpenRouter基準で継続）: %s", exc)
        return None

    logger.info("ModelGrep モデル取得件数: %d", len(models))
    return models


def apply_modelgrep_overlay(
    openrouter_models: list[dict[str, Any]],
    modelgrep_models: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if not modelgrep_models:
        return openrouter_models

    mg_by_id: dict[str, dict[str, Any]] = {}
    for mg in modelgrep_models:
        if isinstance(mg, dict):
            model_id = str(mg.get("id") or "").strip()
            if model_id:
                mg_by_id[model_id] = mg

    for model in openrouter_models:
        if isinstance(model, dict):
            model_id = str(model.get("id") or "").strip()
            mg = mg_by_id.get(model_id)
            if mg is not None:
                model["_modelgrep"] = mg

    return openrouter_models


# =============================================================================
# [E1] Parallel API fetching
# =============================================================================

def fetch_all_data() -> tuple[
    list[dict[str, Any]] | None,
    set[str],
    dict[str, int],
    list[dict[str, Any]] | None,
]:
    """
    Fetch all 4 external API data sources in parallel using threads.
    Returns (openrouter_models, discounted_slugs, popularity_ranks, modelgrep_models).
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        future_models = executor.submit(fetch_openrouter_models)
        future_discounts = executor.submit(fetch_openrouter_discounted_models)
        future_rankings = executor.submit(fetch_openrouter_usage_rankings)
        future_modelgrep = executor.submit(fetch_modelgrep_models)

        models = future_models.result()
        discounts = future_discounts.result()
        rankings = future_rankings.result()
        modelgrep = future_modelgrep.result()

    return models, discounts, rankings, modelgrep


# =============================================================================
# [E2] Pre-computed sort keys
# =============================================================================

@dataclass(frozen=True)
class _ModelSortKey:
    """Pre-computed sort fields for a single model candidate."""
    model_id: str
    is_discounted: bool
    pop_rank: int
    intelligence: float
    created: int
    total_price: float


def _precompute_sort_keys(
    candidates: list[dict[str, Any]],
    popularity_ranks: dict[str, int],
    discounted_slugs: set[str],
) -> dict[str, _ModelSortKey]:
    """Build a lookup of pre-computed sort keys keyed by model id."""
    keys: dict[str, _ModelSortKey] = {}
    for m in candidates:
        mid = str(m.get("id") or "")
        if mid and mid not in keys:
            keys[mid] = _ModelSortKey(
                model_id=mid,
                is_discounted=mid in discounted_slugs,
                pop_rank=popularity_rank(m, popularity_ranks),
                intelligence=mg_aa(m, "intelligence") or 0.0,
                created=get_created(m),
                total_price=total_price(m),
            )
    return keys


# =============================================================================
# Category Selection & Rankings
# =============================================================================

def build_category_rankings(
    models: list[dict[str, Any]],
    fixed_model_ids: set[str],
    popularity_ranks: dict[str, int],
    discounted_slugs: set[str],
) -> dict[str, list[dict[str, Any]]]:
    """
    Select TOP_N (3) models for each category.
    """
    # [E3] Pre-cache text fields for all models.
    for model in models:
        _cache_model_text(model)

    valid_candidates: list[dict[str, Any]] = []
    for model in models:
        model_id = str(model.get("id") or "").strip()
        if not model_id or model_id in fixed_model_ids:
            continue
        if not is_interactive_model(model):
            continue
        if is_always_free_model(model):
            continue
        valid_candidates.append(model)

    # [E2] Pre-compute sort keys once for all candidates.
    sort_keys = _precompute_sort_keys(
        valid_candidates, popularity_ranks, discounted_slugs
    )

    def _get_key(m: dict[str, Any]) -> _ModelSortKey:
        return sort_keys[str(m.get("id") or "")]

    results: dict[str, list[dict[str, Any]]] = {name: [] for name in CATEGORIES}

    # -------------------------------------------------------------------------
    # 1. Popularity Category
    # -------------------------------------------------------------------------
    pop_candidates = list(valid_candidates)
    pop_candidates.sort(
        key=lambda m: (
            not (k := _get_key(m)).is_discounted,  # True sorts after False
            k.pop_rank,
            -k.created,
            k.total_price,
            k.model_id,
        )
    )
    results["popularity"] = pop_candidates[:TOP_N]

    # -------------------------------------------------------------------------
    # 2. Practical Category (High capability + Popularity)
    # -------------------------------------------------------------------------
    prac_candidates = [m for m in valid_candidates if is_high_capability_model(m)]
    if not prac_candidates:
        prac_candidates = list(valid_candidates)

    prac_candidates.sort(
        key=lambda m: (
            not (k := _get_key(m)).is_discounted,
            k.pop_rank,
            -k.intelligence,
            -k.created,
            k.total_price,
            k.model_id,
        )
    )
    results["practical"] = prac_candidates[:TOP_N]

    # -------------------------------------------------------------------------
    # 3. Great Deal Category (Special price + Low cost + Popularity)
    # -------------------------------------------------------------------------
    deal_candidates = [
        m for m in valid_candidates if _get_key(m).is_discounted
    ]

    # [E4] Use set for O(1) existence checks during supplementation.
    deal_ids: set[str] = {str(m.get("id") or "") for m in deal_candidates}

    if len(deal_candidates) < TOP_N:
        supplement = [
            m
            for m in valid_candidates
            if str(m.get("id") or "") not in deal_ids
            and sale_price(m)[0] <= BARGAIN_PRICE_INPUT_CEILING
        ]
        deal_candidates.extend(supplement)
        deal_ids.update(str(m.get("id") or "") for m in supplement)

    if len(deal_candidates) < TOP_N:
        deal_candidates.extend(
            m for m in valid_candidates
            if str(m.get("id") or "") not in deal_ids
        )

    deal_candidates.sort(
        key=lambda m: (
            not (k := _get_key(m)).is_discounted,
            k.total_price,
            k.pop_rank,
            -k.created,
            k.model_id,
        )
    )
    results["great_deal"] = deal_candidates[:TOP_N]

    return results


# =============================================================================
# Config Entry Builders
# =============================================================================

def make_individual_model_entry(
    model: dict[str, Any],
    prefix: str,
) -> dict[str, Any]:
    model_id = str(model["id"])
    return {
        "model_name": model_id_to_name(model_id, prefix=prefix),
        "litellm_params": {
            "model": f"openrouter/{model_id}",
            "api_key": "os.environ/OPENROUTER_API_KEY",
        },
    }


def make_public_group_entry(
    group_name: str,
    primary_model: dict[str, Any],
) -> dict[str, Any]:
    model_id = str(primary_model["id"])
    cat = CATEGORIES.get(group_name)
    title = cat.display_title if cat else group_name

    model_info: dict[str, Any] = {
        "description": f"Dynamic {title} primary: {primary_model.get('name') or model_id}",
        "supports_vision": is_vision_model(primary_model),
    }

    context_length = get_context_length(primary_model)
    if context_length > 0:
        model_info["max_input_tokens"] = context_length

    mg_price = mg_pricing(primary_model)
    if mg_price is not None:
        input_per_m, output_per_m = mg_price
        model_info["input_cost_per_token"] = input_per_m / 1_000_000.0
        model_info["output_cost_per_token"] = output_per_m / 1_000_000.0
    else:
        pricing = primary_model.get("pricing") or {}
        try:
            model_info["input_cost_per_token"] = float(pricing.get("prompt") or 0.0)
            model_info["output_cost_per_token"] = float(
                pricing.get("completion") or 0.0
            )
        except (TypeError, ValueError):
            pass

    return {
        "model_name": group_name,
        "model_info": model_info,
        "litellm_params": {
            "model": f"openrouter/{model_id}",
            "api_key": "os.environ/OPENROUTER_API_KEY",
        },
    }


# =============================================================================
# Build Dynamic Config Elements
# =============================================================================

def build_dynamic_entries(
    models: list[dict[str, Any]],
    fixed_model_ids: set[str],
    popularity_ranks: dict[str, int],
    discounted_slugs: set[str],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, list[str]]],
    dict[str, list[str]],
    list[dict[str, Any]],
]:
    category_rankings = build_category_rankings(
        models, fixed_model_ids, popularity_ranks, discounted_slugs
    )

    model_list_entries: list[dict[str, Any]] = []
    fallback_rules: list[dict[str, list[str]]] = []
    selection_summary: dict[str, list[str]] = {}
    notification_items: list[dict[str, Any]] = []

    for group_name, candidates in category_rankings.items():
        if not candidates:
            logger.warning("[%s] 候補モデルが見つかりませんでした", group_name)
            continue

        primary_model = candidates[0]

        # 1. Representative Group Entry
        model_list_entries.append(
            make_public_group_entry(group_name, primary_model)
        )

        fallback_targets: list[str] = []
        summary_ids: list[str] = []

        for rank, candidate in enumerate(candidates, start=1):
            model_id = str(candidate["id"])
            direct_name = model_id_to_name(model_id, prefix=group_name)
            summary_ids.append(model_id)

            # 2. Individual Entry for each candidate
            model_list_entries.append(
                make_individual_model_entry(candidate, prefix=group_name)
            )

            # Record for notification
            notification_items.append(
                {
                    "group": group_name,
                    "rank": rank,
                    "model": candidate,
                    "direct_name": direct_name,
                }
            )

            # Add to fallback targets (rank 2 & 3)
            if rank > 1:
                fallback_targets.append(direct_name)

        if fallback_targets:
            fallback_rules.append({group_name: fallback_targets})

        selection_summary[group_name] = summary_ids

        logger.info(
            "[%s] Primary: %s (Fallbacks: %s)",
            group_name,
            primary_model.get("id"),
            ", ".join(fallback_targets) if fallback_targets else "なし",
        )
        for idx, m_id in enumerate(summary_ids, start=1):
            logger.info("  %d. %s", idx, m_id)

    return (
        model_list_entries,
        fallback_rules,
        selection_summary,
        notification_items,
    )


# =============================================================================
# Discord Notification
# =============================================================================

def _validate_webhook_url(url: str) -> bool:
    """
    [S1] Validate that the Discord webhook URL points to a legitimate
    Discord endpoint, preventing SSRF attacks against internal services.
    """
    return url.startswith(_DISCORD_WEBHOOK_PREFIXES)


def _truncate_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}…"


def _notification_line(item: dict[str, Any]) -> str:
    model = item["model"]
    rank = item["rank"]
    direct_name = item["direct_name"]
    model_id = str(model.get("id") or "unknown")

    input_cost, output_cost = sale_price(model)
    input_text = f"${input_cost:.3f}" if math.isfinite(input_cost) else "N/A"
    output_text = f"${output_cost:.3f}" if math.isfinite(output_cost) else "N/A"

    type_tag = "👁️ Vision" if is_vision_model(model) else "📝 Text"
    intel = mg_aa(model, "intelligence")
    intel_text = f"知性: {intel:.1f}" if intel is not None else ""

    features = [type_tag]
    if intel_text:
        features.append(intel_text)
    if is_coder_model(model):
        features.append("💻 Code")

    desc = _truncate_text(model.get("description"), 90) or "説明なし"

    return (
        f"**#{rank} {model_id}** (`{direct_name}`)\n"
        f"💰 1M: 入力 {input_text} / 出力 {output_text} ｜ 🏷️ {' / '.join(features)}\n"
        f"📖 {desc}"
    )


def send_discord_notification(
    notification_items: list[dict[str, Any]],
) -> None:
    webhook_url = os.environ.get(DISCORD_WEBHOOK_ENV, "").strip()
    if not webhook_url:
        logger.info("DISCORD_WEBHOOK_URL 未設定のため通知をスキップします")
        return

    # [S1] SSRF prevention: only allow known Discord webhook endpoints.
    if not _validate_webhook_url(webhook_url):
        logger.error(
            "DISCORD_WEBHOOK_URL が正規の Discord Webhook URL ではありません "
            "(https://discord.com/api/webhooks/ で始まる必要があります)。通知をスキップします。"
        )
        return

    group_colors = {
        "popularity": 15158332,  # Red/Orange
        "practical": 3447003,    # Blue
        "great_deal": 3066993,   # Green
    }

    items_by_group: dict[str, list[dict[str, Any]]] = {
        name: [] for name in CATEGORIES
    }
    for item in notification_items:
        g = item.get("group")
        if g in items_by_group:
            items_by_group[g].append(item)

    for group_name, items in items_by_group.items():
        if not items:
            continue

        cat = CATEGORIES.get(group_name)
        title = cat.display_title if cat else group_name
        color = group_colors.get(group_name, 5793266)

        items.sort(key=lambda it: it["rank"])
        lines = [_notification_line(it) for it in items]

        embed = {
            "title": title,
            "description": f"*{cat.description}*\n\n" + "\n\n".join(lines),
            "color": color,
        }

        payload = {
            "content": f"📢 **LiteLLM モデル更新: [{group_name}]**",
            "embeds": [embed],
        }

        request_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            request = urllib.request.Request(
                webhook_url,
                data=request_body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "LiteLLM-Notifier/5.1",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                _safe_read(response)
            logger.info("Discord 通知送信成功: [%s]", group_name)
        except Exception as exc:
            logger.warning("Discord 通知失敗 [%s]: %s", group_name, exc)


# =============================================================================
# Cleanup & Merge
# =============================================================================

def remove_dynamic_entries(model_list: list[Any]) -> list[Any]:
    cleaned: list[Any] = []
    for entry in model_list:
        if not isinstance(entry, dict):
            cleaned.append(entry)
            continue

        model_name = str(entry.get("model_name") or "")

        # Remove public group names
        if model_name in PUBLIC_GROUP_NAMES:
            continue

        # [E5] Remove dynamically prefixed names with pre-built tuple.
        if model_name.startswith(_DYNAMIC_PREFIXES) or model_name.startswith(
            "openrouter-"
        ):
            continue

        cleaned.append(entry)
    return cleaned


def remove_dynamic_fallbacks(fallbacks: list[Any]) -> list[Any]:
    cleaned: list[Any] = []
    for rule in fallbacks:
        if not isinstance(rule, dict):
            cleaned.append(rule)
            continue
        if any(source in PUBLIC_GROUP_NAMES for source in rule):
            continue
        cleaned.append(rule)
    return cleaned


def merge_config(
    base_config: dict[str, Any],
    dynamic_entries: list[dict[str, Any]],
    dynamic_fallbacks: list[dict[str, list[str]]],
) -> dict[str, Any]:
    merged = copy.deepcopy(base_config)

    existing_model_list = merged.get("model_list") or []
    if not isinstance(existing_model_list, list):
        existing_model_list = []

    cleaned_models = remove_dynamic_entries(existing_model_list)
    merged["model_list"] = cleaned_models + dynamic_entries

    router_settings = merged.setdefault("router_settings", {})
    if not isinstance(router_settings, dict):
        raise TypeError("router_settings must be a mapping")

    existing_fallbacks = router_settings.get("fallbacks") or []
    if not isinstance(existing_fallbacks, list):
        existing_fallbacks = []

    cleaned_fallbacks = remove_dynamic_fallbacks(existing_fallbacks)
    router_settings["fallbacks"] = cleaned_fallbacks + dynamic_fallbacks

    merged.pop("model_group_alias", None)
    return merged


def validate_config(config: dict[str, Any]) -> None:
    model_list = config.get("model_list")
    if not isinstance(model_list, list):
        raise TypeError("model_list must be a list")

    model_names = {
        str(entry.get("model_name"))
        for entry in model_list
        if isinstance(entry, dict)
    }

    for group_name in PUBLIC_GROUP_NAMES:
        if group_name not in model_names:
            logger.warning("Group '%s' is missing in generated config", group_name)


# =============================================================================
# Main Execution
# =============================================================================

def main() -> int:
    load_env_file()

    if not BASE_YAML_PATH.exists():
        logger.error("ベース設定ファイル %s が見つかりません", BASE_YAML_PATH)
        return 1

    try:
        with BASE_YAML_PATH.open("r", encoding="utf-8") as handle:
            base_config = yaml.safe_load(handle) or {}
    except Exception as exc:
        logger.error("ベース設定ファイル読み込み失敗: %s", exc)
        return 1

    fixed_ids = fixed_openrouter_model_ids(base_config)

    # [E1] Fetch all 4 external APIs in parallel.
    models, discounted_slugs, popularity_ranks, mg_models = fetch_all_data()

    if not models:
        logger.error("OpenRouter カタログ取得失敗により更新を中断します")
        return 1

    models = apply_modelgrep_overlay(models, mg_models)

    # Build dynamic configuration elements
    (
        dynamic_entries,
        dynamic_fallbacks,
        _,
        notification_items,
    ) = build_dynamic_entries(
        models, fixed_ids, popularity_ranks, discounted_slugs
    )

    # Merge & Validate
    merged_config = merge_config(base_config, dynamic_entries, dynamic_fallbacks)
    validate_config(merged_config)

    # [S2/S3] Atomic write with restrictive file permissions.
    output_dir = OUTPUT_YAML_PATH.parent.resolve()
    fd, temp_path_str = tempfile.mkstemp(
        prefix="config.", suffix=".yaml.tmp", dir=str(output_dir), text=True
    )
    temp_path = Path(temp_path_str)

    try:
        # [S2] Immediately restrict permissions before writing any content.
        os.fchmod(fd, _CONFIG_FILE_MODE)

        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(
                merged_config,
                handle,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
                indent=2,
            )

        # Verification read
        with temp_path.open("r", encoding="utf-8") as handle:
            parsed = yaml.safe_load(handle)
        if not isinstance(parsed, dict):
            raise TypeError("Generated YAML is not a mapping")

        temp_path.replace(OUTPUT_YAML_PATH)

        # [S3] Ensure final file also has restricted permissions.
        OUTPUT_YAML_PATH.chmod(_CONFIG_FILE_MODE)

        logger.info("設定ファイルを正常に更新しました: %s", OUTPUT_YAML_PATH)
    except Exception as exc:
        logger.error("設定ファイル書き込み失敗: %s", exc)
        if temp_path.exists():
            temp_path.unlink()
        return 1

    # Send Discord Notification
    send_discord_notification(notification_items)

    return 0


if __name__ == "__main__":
    sys.exit(main())
