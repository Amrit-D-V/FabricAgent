from __future__ import annotations

import re

from core.cache import ttl_cache
from config import (
    LLM_PICKER_CATALOG,
    OLLAMA_BASE_URL,
    OPENAI_COMPATIBLE_PROVIDERS,
)
from llm.llm_client import get_model_name, get_provider, resolve_provider_api_key


def _provider_has_key(provider: str) -> bool:
    if provider in OPENAI_COMPATIBLE_PROVIDERS or provider == "anthropic":
        return bool(resolve_provider_api_key(provider))
    return True


def _default_label() -> str:
    cur = get_provider("default")
    for label, spec in LLM_PICKER_CATALOG.items():
        if spec["provider"] == cur:
            return label
    return next(iter(LLM_PICKER_CATALOG))


def _ollama_models() -> list[str]:
    try:
        import requests

        base = OLLAMA_BASE_URL.rstrip("/")
        base = base.removesuffix("/v1")
        resp = requests.get(f"{base}/api/tags", timeout=2)
        names = [m["name"] for m in resp.json().get("models", []) if m.get("name")]
        if names:
            return names
    except Exception:
        pass
    return list(LLM_PICKER_CATALOG["Ollama (local)"]["models"])


_CUSTOM = "Custom model…"

_NONCHAT = (
    "embedding",
    "embed",
    "whisper",
    "tts",
    "audio",
    "playai",
    "dall-e",
    "imagen",
    "-image",
    "image-",
    "aqa",
    "guard",
    "moderation",
    "rerank",
    "compound",
    "babbage",
    "davinci",
    "curie",
    "realtime",
    "transcribe",
    "turbo-instruct",
)

_SNAP = re.compile(
    r"-(?:\d{4}-\d{2}-\d{2}|\d{8}|\d{3,4}|(?:preview|latest|exp)(?:-[\d-]+)?)$"
)


@ttl_cache(600)
def live_models(provider: str, has_key: bool) -> list[str]:
    cfg = OPENAI_COMPATIBLE_PROVIDERS.get(provider)
    if not cfg or not has_key:
        return []
    try:
        import requests

        base = (cfg["base_url"] or "https://api.openai.com/v1").rstrip("/")
        key = resolve_provider_api_key(provider)
        resp = requests.get(
            f"{base}/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=8,
        )
        resp.raise_for_status()
        out: list[str] = []
        for m in resp.json().get("data", []):
            if m.get("active") is False:
                continue
            mid = (m.get("id") or "").strip()
            mid = mid.removeprefix("models/")
            if mid and not any(x in mid.lower() for x in _NONCHAT):
                out.append(mid)
        ids = set(out)
        return sorted(
            m for m in ids if not ((base := _SNAP.sub("", m)) != m and base in ids)
        )
    except Exception:
        return []


def picker_options() -> list[dict]:
    """The model choices the UI offers, one entry per catalogue provider.

    Replaces render_llm_picker(): the catalogue and the live model listing are
    data, so the API serves them and React draws the control.
    """
    out: list[dict] = []
    for label, spec in LLM_PICKER_CATALOG.items():
        provider = spec["provider"]
        has_key = _provider_has_key(provider)
        if provider == "ollama":
            models = _ollama_models()
        else:
            models = live_models(provider, has_key) or list(spec.get("models", []))
        out.append(
            {
                "label": label,
                "provider": provider,
                "models": models,
                "hasKey": has_key,
                "default": label == _default_label(),
            }
        )
    return out
