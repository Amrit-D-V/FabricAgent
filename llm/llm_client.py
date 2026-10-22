import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable

from dotenv import load_dotenv

load_dotenv()

from config import OPENAI_COMPATIBLE_PROVIDERS
from llm.tool_evidence import ClaudeToolCollector, ToolEvidence

LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "anthropic").lower()
OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:latest")
ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
OLLAMA_NUM_CTX: int = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
OLLAMA_MAX_TOKENS: int = int(os.getenv("OLLAMA_MAX_TOKENS", "3072"))
HUGGINGFACE_MODEL: str = os.getenv(
    "HUGGINGFACE_MODEL", "Qwen/Qwen2.5-Coder-32B-Instruct"
)
HF_TOKEN: str = os.getenv("HF_TOKEN", "")
HF_MAX_NEW_TOKENS: int = int(os.getenv("HF_MAX_NEW_TOKENS", "8192"))
HF_ROUTER_MODEL: str = os.getenv(
    "HF_ROUTER_MODEL", "deepseek-ai/DeepSeek-V4-Pro:novita"
)

SEMANTIC_MODEL_PROVIDER: str = os.getenv(
    "SEMANTIC_MODEL_PROVIDER", LLM_PROVIDER
).lower()
DASHBOARD_PROVIDER: str = os.getenv("DASHBOARD_PROVIDER", LLM_PROVIDER).lower()
WIREFRAME_PROVIDER: str = os.getenv("WIREFRAME_PROVIDER", LLM_PROVIDER).lower()

SMALL_TASK_PROVIDER: str = os.getenv("SMALL_TASK_PROVIDER", LLM_PROVIDER).lower()
BIG_TASK_PROVIDER: str = os.getenv("BIG_TASK_PROVIDER", LLM_PROVIDER).lower()

_TIER_ROUTE: dict[str, dict[str, str]] = {
    "mechanical": {
        "provider": os.getenv("LLM_MECHANICAL_PROVIDER", "").lower(),
        "model": os.getenv("LLM_MECHANICAL_MODEL", ""),
    },
    "creative": {
        "provider": os.getenv("LLM_CREATIVE_PROVIDER", "").lower(),
        "model": os.getenv("LLM_CREATIVE_MODEL", ""),
    },
}
_AGENT_TIERS: dict[str, str] = {
    "docs": "mechanical",
    "doc": "mechanical",
    "model_docs": "mechanical",
    "business_rules": "mechanical",
    "summary": "mechanical",
    "memory": "mechanical",
    "rename": "mechanical",
    "small_task": "mechanical",
    "semantic_model": "creative",
    "measure": "creative",
    "dashboard": "creative",
    "wireframe_html": os.getenv("WIREFRAME_HTML_TIER", "creative"),
    "wireframe": "creative",
    "design_judge": "creative",
    "big_task": "creative",
}
_DEFAULT_TIER: str = os.getenv("LLM_DEFAULT_TIER", "creative")


def _tier_of(agent: str) -> str:
    best, tier = "", _DEFAULT_TIER
    for name, t in _AGENT_TIERS.items():
        if (agent == name or agent.startswith(name)) and len(name) > len(best):
            best, tier = name, t
    return tier


def _tier_route(agent: str) -> dict | None:
    r = _TIER_ROUTE.get(_tier_of(agent)) or {}
    return (
        {"provider": r["provider"], "model": r.get("model", "")}
        if r.get("provider")
        else None
    )


@dataclass
class UsageInfo:
    input_tokens: int
    output_tokens: int
    model: str


_LLM_OVERRIDES_TLS = threading.local()


def _overrides() -> dict[str, dict]:
    d = getattr(_LLM_OVERRIDES_TLS, "d", None)
    if d is None:
        d = _LLM_OVERRIDES_TLS.d = {}
    return d


def set_llm_override(agent: str, provider: str, model: str) -> None:
    _overrides()[agent] = {"provider": (provider or "").lower(), "model": model}


def clear_llm_override(agent: str) -> None:
    _overrides().pop(agent, None)


def _get_override(agent: str) -> dict | None:
    return _overrides().get(agent)


@contextmanager
def llm_override(agents, provider: str, model: str):
    if isinstance(agents, str):
        agents = [agents]
    for a in agents:
        set_llm_override(a, provider, model)
    try:
        yield
    finally:
        for a in agents:
            clear_llm_override(a)


def _get_active_project_llm() -> dict | None:
    project_id = None
    # The active project is set per request by the API middleware; audit_context
    # is where it lives, so this is the single source for it.
    try:
        from db.audit_context import get_context

        project_id = get_context().get("project_id")
    except Exception:
        pass
    if project_id:
        try:
            from db.database import get_project_llm_config

            return get_project_llm_config(int(project_id))
        except Exception:
            pass
    return None


def get_provider(agent: str = "default") -> str:
    ov = _get_override(agent)
    if ov and ov.get("provider"):
        return ov["provider"]
    proj_llm = _get_active_project_llm()
    if proj_llm and proj_llm.get("provider"):
        return proj_llm["provider"]
    tr = _tier_route(agent)
    if tr:
        return tr["provider"]
    if agent.endswith("_small") or agent == "small_task":
        return SMALL_TASK_PROVIDER
    if agent.endswith("_big") or agent == "big_task":
        return BIG_TASK_PROVIDER
    if agent == "semantic_model":
        return SEMANTIC_MODEL_PROVIDER
    if agent == "dashboard":
        return DASHBOARD_PROVIDER
    if agent == "wireframe":
        return WIREFRAME_PROVIDER
    return LLM_PROVIDER


def get_model_name(agent: str = "default") -> str:
    ov = _get_override(agent)
    if ov and ov.get("model"):
        return ov["model"]
    proj_llm = _get_active_project_llm()
    if proj_llm and proj_llm.get("model"):
        return proj_llm["model"]
    tr = _tier_route(agent)
    if tr and tr.get("model"):
        return tr["model"]
    if agent == "wireframe_html":
        wf_html_model = os.getenv("WIREFRAME_HTML_MODEL")
        if wf_html_model:
            return wf_html_model
    provider = get_provider(agent)
    if provider in OPENAI_COMPATIBLE_PROVIDERS:
        return OPENAI_COMPATIBLE_PROVIDERS[provider]["default_model"]
    if provider == "ollama":
        return OLLAMA_MODEL
    if provider == "huggingface":
        return HUGGINGFACE_MODEL
    if provider == "hf_router":
        return HF_ROUTER_MODEL
    if provider == "claude_cli":
        return os.getenv("CLAUDE_CLI_MODEL", "claude-sonnet-4-6")
    return ANTHROPIC_MODEL


def resolve_provider_api_key(provider: str) -> str:
    try:
        from db.database import get_org_llm_key

        k = get_org_llm_key(provider)
        if k:
            return k
    except Exception:
        pass
    cfg = OPENAI_COMPATIBLE_PROVIDERS.get(provider)
    env_name = (
        cfg["api_key_env"]
        if cfg
        else {"anthropic": "ANTHROPIC_API_KEY"}.get(provider, "")
    )
    return os.getenv(env_name, "") if env_name else ""


def friendly_provider_error(provider: str, model: str, exc: Exception) -> str:
    m = str(exc)
    low = m.lower()
    who = provider or "the provider"
    if any(
        k in low
        for k in (
            "insufficient_quota",
            "exceeded your current quota",
            "plan and billing",
            "billing details",
            "check your plan",
        )
    ):
        return (
            f"{who} ({model}) rejected this as a billing/quota problem — the account "
            f"is out of credits/quota, NOT a prompt-size issue. Add credits or upgrade "
            f"the plan in the {who} billing dashboard, or switch to Claude (the Claude "
            f"Code CLI option needs no API credits). [{m[:200]}]"
        )
    size_or_rate = any(
        k in low
        for k in (
            "request too large",
            "tokens per minute",
            "tpm",
            "rate_limit",
            "rate limit",
            " 413",
            "code: 413",
            "429",
            "context length",
            "maximum context",
            "too many tokens",
        )
    )
    if size_or_rate:
        return (
            f"{who} ({model}) rejected this request as too large for your account's "
            f"size/rate limit. This app sends big prompts (model metadata + schema), so "
            f"small-context or free-tier providers (e.g. Groq free = 12k tokens/min) "
            f"can't handle the builder/pipelines. Use a large-context provider "
            f"(Claude, OpenAI, or Gemini) for this task, or upgrade your {who} tier. "
            f"[{m[:200]}]"
        )
    return f"{who} ({model}) error: {m[:300]}"


def stream_text(
    system: str,
    user: str,
    max_tokens: int = 32000,
    agent: str = "default",
    temperature: float | None = None,
    allow_read: bool = False,
    provider: str | None = None,
    model: str | None = None,
    allowed_tools: str | None = None,
    disallowed_tools: str | None = None,
    mcp_config: str | None = None,
    strict_mcp: bool = False,
    block_separator: str | None = None,
    extra_env: dict | None = None,
    on_tool_event: Callable[[ToolEvidence], None] | None = None,
):
    """Stream a completion.

    `provider`/`model` override the agent's configured pair — used to try a
    selection before it is saved. Omit both for normal agent routing.

    `allowed_tools`/`disallowed_tools`/`mcp_config`/`strict_mcp` apply to the
    `claude_cli` provider only, and let a caller narrow the CLI's tool surface
    below the default. Callers exposed to a network (see chat_api_server.py)
    should pass an explicit allowlist rather than relying on the default deny.

    `on_tool_event` receives structured execution evidence when the provider
    supports tools. Text and UsageInfo yields remain backward compatible.
    """
    provider = provider or get_provider(agent)
    resolved_model = model or get_model_name(agent)
    if provider == "ollama":
        yield from _stream_ollama(
            system,
            user,
            max_tokens,
            temperature=temperature,
            model=resolved_model,
        )
    elif provider in OPENAI_COMPATIBLE_PROVIDERS:
        cfg = OPENAI_COMPATIBLE_PROVIDERS[provider]
        yield from _stream_openai_compatible(
            system,
            user,
            max_tokens,
            temperature=temperature,
            base_url=cfg["base_url"],
            api_key=resolve_provider_api_key(provider),
            model=resolved_model,
            provider=provider,
        )
    elif provider == "huggingface":
        yield from _stream_huggingface(
            system, user, max_tokens, temperature=temperature
        )
    elif provider == "hf_router":
        yield from _stream_hf_router(system, user, max_tokens, temperature=temperature)
    elif provider == "claude_cli":
        ov = _get_override(agent)
        yield from _stream_claude_cli(
            system,
            user,
            max_tokens,
            allow_read=allow_read,
            model=model or (ov.get("model") if ov else None),
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            mcp_config=mcp_config,
            strict_mcp=strict_mcp,
            block_separator=block_separator,
            extra_env=extra_env,
            on_tool_event=on_tool_event,
        )
    elif provider == "codex_cli":
        ov = _get_override(agent)
        yield from _stream_codex_cli(
            system,
            user,
            max_tokens,
            model=model or (ov.get("model") if ov else None),
        )
    else:
        yield from _stream_anthropic(
            system,
            user,
            max_tokens,
            temperature=temperature,
            model=resolved_model,
        )


_CLAUDE_BIN_CACHE: str | None = None


def _find_claude_bin() -> str:
    global _CLAUDE_BIN_CACHE
    if _CLAUDE_BIN_CACHE:
        return _CLAUDE_BIN_CACHE
    import shutil

    found = (
        shutil.which("claude")
        or shutil.which("claude.cmd")
        or shutil.which("claude.exe")
    )
    if not found:
        raise RuntimeError(
            "`claude` CLI not found in PATH. Install Claude Code "
            "(https://docs.claude.com/en/docs/claude-code/setup) or switch provider."
        )
    _CLAUDE_BIN_CACHE = found
    return found


def _spawn_cli_process(
    cmd: list[str],
    stdin_text: str,
    env: dict | None = None,
    cwd: str | None = None,
    merge_stderr: bool = False,
):
    import subprocess

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        cwd=cwd,
    )

    def _feed():
        try:
            if proc.stdin:
                proc.stdin.write(stdin_text)
                proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    threading.Thread(target=_feed, daemon=True).start()

    if merge_stderr:
        return proc, None, None

    stderr_chunks: list[str] = []

    def _drain_stderr():
        try:
            if proc.stderr:
                stderr_chunks.extend(iter(proc.stderr.readline, ""))
        except Exception:
            pass

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    return proc, stderr_thread, stderr_chunks


def _stream_claude_cli(
    system: str,
    user: str,
    max_tokens: int,
    allow_read: bool = False,
    model: str | None = None,
    allowed_tools: str | None = None,
    disallowed_tools: str | None = None,
    mcp_config: str | None = None,
    strict_mcp: bool = False,
    block_separator: str | None = None,
    extra_env: dict | None = None,
    on_tool_event: Callable[[ToolEvidence], None] | None = None,
):
    import json
    import logging
    import os

    log = logging.getLogger(__name__)
    claude_bin = _find_claude_bin()
    model = model or os.getenv("CLAUDE_CLI_MODEL", "claude-sonnet-4-6")

    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    env.update({k: str(v) for k, v in (extra_env or {}).items()})

    if env.get("AZURE_CLIENT_SECRET") and not env.get("AZURE_TOKEN_CREDENTIALS"):
        env["AZURE_TOKEN_CREDENTIALS"] = "prod"

    full_prompt = f"{system}\n\n---\n\n{user}" if system else user

    from config import CLAUDE_CLI_DISALLOWED_TOOLS

    _denied = [t for t in CLAUDE_CLI_DISALLOWED_TOOLS if not (allow_read and t == "Read")]
    disallowed = disallowed_tools or ",".join(_denied)
    _tools_wanted = bool(allowed_tools) or allow_read

    cmd = [
        claude_bin,
        "-p",
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--disallowedTools",
        disallowed,
    ]

    if _tools_wanted:
        cmd += ["--permission-mode", "bypassPermissions"]
        cmd += ["--allowedTools", allowed_tools or "Read"]
    else:
        cmd += ["--permission-mode", "default"]
    if mcp_config:
        cmd += ["--mcp-config", mcp_config]
        if strict_mcp:
            cmd += ["--strict-mcp-config"]
    proc, stderr_thread, stderr_chunks = _spawn_cli_process(cmd, full_prompt, env=env)

    usage_in = 0
    usage_out = 0
    actual_model = model
    last_err: str | None = None
    emitted_text = False
    tool_collector = ClaudeToolCollector() if on_tool_event else None

    if proc.stdout:
        for raw_line in iter(proc.stdout.readline, ""):
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                log.debug(f"[claude-cli non-json] {line[:200]}")
                continue

            if not isinstance(obj, dict):
                continue
            if tool_collector is not None and on_tool_event is not None:
                for evidence in tool_collector.feed(obj):
                    on_tool_event(evidence)

            t = obj.get("type")
            if t == "system":
                sub = obj.get("subtype", "")
                if sub == "init":
                    actual_model = obj.get("model") or actual_model
                    log.info(
                        f"[claude-cli] init model={actual_model} session={obj.get('session_id', '?')[:8]}"
                    )
                elif sub == "status":
                    log.info(f"[claude-cli] status={obj.get('status')}")
            elif t == "rate_limit_event":
                info = obj.get("rate_limit_info", {})
                if info.get("status") != "allowed":
                    log.warning(f"[claude-cli] rate_limit={info}")
            elif t == "stream_event":
                ev = obj.get("event", {})
                if ev.get("type") == "content_block_start":
                    if (
                        block_separator
                        and emitted_text
                        and (ev.get("content_block") or {}).get("type") == "text"
                    ):
                        yield block_separator
                elif ev.get("type") == "content_block_delta":
                    delta = ev.get("delta", {})
                    if delta.get("type") == "text_delta":
                        txt = delta.get("text", "")
                        if txt:
                            emitted_text = True
                            yield txt
                elif ev.get("type") == "message_start":
                    msg = ev.get("message", {})
                    u = msg.get("usage", {}) or {}
                    if u:
                        usage_in = u.get("input_tokens", usage_in)
            elif t == "result":
                if obj.get("is_error"):
                    last_err = obj.get("result") or str(obj)
                u = obj.get("usage", {}) or {}
                if u:
                    usage_in = u.get("input_tokens", usage_in)
                    usage_out = u.get("output_tokens", usage_out)
                dur = obj.get("duration_ms")
                cost = obj.get("total_cost_usd")
                log.info(
                    f"[claude-cli] result duration={dur}ms cost=${cost} "
                    f"tokens in={usage_in} out={usage_out}"
                )

    rc = proc.wait()
    if tool_collector is not None and on_tool_event is not None:
        for evidence in tool_collector.finish():
            on_tool_event(evidence)
    if rc != 0 or last_err:
        if stderr_thread is not None:
            stderr_thread.join(timeout=1.0)
        stderr_tail = ("".join(stderr_chunks or []))[-400:]
        raise RuntimeError(
            f"claude -p exited with code {rc}. {last_err or stderr_tail or ''}"
        )

    yield UsageInfo(input_tokens=usage_in, output_tokens=usage_out, model=actual_model)


_CODEX_BIN_CACHE: str | None = None


def _find_codex_bin() -> str:
    global _CODEX_BIN_CACHE
    if _CODEX_BIN_CACHE:
        return _CODEX_BIN_CACHE
    import shutil

    found = (
        shutil.which("codex") or shutil.which("codex.cmd") or shutil.which("codex.exe")
    )
    if not found:
        raise RuntimeError(
            "`codex` CLI not found in PATH. Install Codex CLI "
            "(npm install -g @openai/codex) or switch provider."
        )
    _CODEX_BIN_CACHE = found
    return found


def _stream_codex_cli(
    system: str,
    user: str,
    max_tokens: int,
    model: str | None = None,
):
    import os

    codex_bin = _find_codex_bin()
    model = model or os.getenv("CODEX_CLI_MODEL", "gpt-5.5")
    if model.startswith(("o1-", "gpt-4")):
        model = "gpt-5.5"

    env = os.environ.copy()

    full_prompt = f"{system}\n\n---\n\n{user}" if system else user

    cmd = [
        codex_bin,
        "exec",
        "-",
        "--model",
        model,
    ]

    proc, stderr_thread, stderr_chunks = _spawn_cli_process(cmd, full_prompt, env=env)

    buffer = []
    if proc.stdout:
        while True:
            chunk = proc.stdout.read(8)
            if not chunk:
                break
            buffer.append(chunk)
            yield chunk

    rc = proc.wait()
    if rc != 0:
        if stderr_thread is not None:
            stderr_thread.join(timeout=1.0)
        stderr_tail = ("".join(stderr_chunks or []))[-400:]
        raise RuntimeError(f"codex exec exited with code {rc}. {stderr_tail or ''}")

    full_text = "".join(buffer)
    usage_in = len(full_prompt) // 4
    usage_out = len(full_text) // 4

    yield UsageInfo(input_tokens=usage_in, output_tokens=usage_out, model=model)


def _stream_anthropic(
    system: str,
    user: str,
    max_tokens: int,
    temperature: float | None = None,
    model: str | None = None,
):
    import anthropic

    client = anthropic.Anthropic(api_key=resolve_provider_api_key("anthropic") or None)
    model = model or ANTHROPIC_MODEL

    system_blocks: Any = (
        [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        if system
        else []
    )

    extra: dict = {}
    if temperature is not None:
        extra["temperature"] = temperature
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system_blocks,
        messages=[{"role": "user", "content": user}],
        **extra,
    ) as stream:
        yield from stream.text_stream
        msg = stream.get_final_message()
        cache_read = getattr(msg.usage, "cache_read_input_tokens", 0) or 0
        cache_create = getattr(msg.usage, "cache_creation_input_tokens", 0) or 0
        if cache_read or cache_create:
            import logging as _log

            _log.getLogger(__name__).info(
                f"[anthropic cache] read={cache_read}, created={cache_create}, "
                f"effective_input={msg.usage.input_tokens} "
                f"(saved ~${(cache_read * 0.9 * 3 / 1_000_000):.4f} on this call)"
            )
        yield UsageInfo(
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
            model=model,
        )


def _stream_ollama(
    system: str,
    user: str,
    max_tokens: int,
    temperature: float | None = None,
    model: str | None = None,
):
    from openai import OpenAI

    model = model or OLLAMA_MODEL
    client = OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama")
    effective_max = min(max_tokens, OLLAMA_MAX_TOKENS)
    extra: dict = {}
    if temperature is not None:
        extra["temperature"] = temperature
    completion: Any = client.chat.completions.create(
        model=model,
        max_tokens=effective_max,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        stream=True,
        stream_options={"include_usage": True},
        extra_body={"options": {"num_ctx": OLLAMA_NUM_CTX}},
        **extra,
    )
    total_in = 0
    total_out = 0
    for chunk in completion:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content
        if chunk.usage:
            total_in = chunk.usage.prompt_tokens or 0
            total_out = chunk.usage.completion_tokens or 0
    yield UsageInfo(input_tokens=total_in, output_tokens=total_out, model=model)


def _stream_openai_compatible(
    system: str,
    user: str,
    max_tokens: int,
    temperature: float | None = None,
    *,
    base_url: str | None,
    api_key: str,
    model: str,
    provider: str = "",
):
    from openai import OpenAI

    client = OpenAI(base_url=base_url or None, api_key=api_key or "")
    extra: dict = {}
    if temperature is not None:
        extra["temperature"] = temperature
    total_in = 0
    total_out = 0
    try:
        completion: Any = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            stream=True,
            stream_options={"include_usage": True},
            **extra,
        )
        for chunk in completion:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
            if getattr(chunk, "usage", None):
                total_in = chunk.usage.prompt_tokens or 0
                total_out = chunk.usage.completion_tokens or 0
    except Exception as e:
        raise RuntimeError(friendly_provider_error(provider, model, e)) from e
    yield UsageInfo(input_tokens=total_in, output_tokens=total_out, model=model)


def _stream_huggingface(
    system: str, user: str, max_tokens: int, temperature: float | None = None
):
    from huggingface_hub import InferenceClient

    client = InferenceClient(model=HUGGINGFACE_MODEL, token=HF_TOKEN or None)
    effective_max = min(max_tokens, HF_MAX_NEW_TOKENS)

    extra: dict = {}
    if temperature is not None:
        extra["temperature"] = temperature
    stream: Any = client.chat_completion(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=effective_max,
        stream=True,
        **extra,
    )

    input_tokens = 0
    output_tokens = 0
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content
        usage = getattr(chunk, "usage", None)
        if usage:
            input_tokens = getattr(usage, "prompt_tokens", None) or input_tokens
            output_tokens = getattr(usage, "completion_tokens", None) or output_tokens

    yield UsageInfo(
        input_tokens=input_tokens, output_tokens=output_tokens, model=HUGGINGFACE_MODEL
    )


def _stream_hf_router(
    system: str, user: str, max_tokens: int, temperature: float | None = None
):
    from openai import OpenAI

    client = OpenAI(
        base_url="https://router.huggingface.co/v1",
        api_key=HF_TOKEN,
    )
    extra: dict = {}
    if temperature is not None:
        extra["temperature"] = temperature
    completion: Any = client.chat.completions.create(
        model=HF_ROUTER_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=min(max_tokens, 16000),
        stream=True,
        stream_options={"include_usage": True},
        **extra,
    )
    total_in = 0
    total_out = 0
    for chunk in completion:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content
        if hasattr(chunk, "usage") and chunk.usage:
            total_in = chunk.usage.prompt_tokens or 0
            total_out = chunk.usage.completion_tokens or 0
    yield UsageInfo(
        input_tokens=total_in, output_tokens=total_out, model=HF_ROUTER_MODEL
    )


def call_with_images(
    system: str,
    text: str,
    images: list[dict],
    max_tokens: int = 4096,
    agent: str = "default",
) -> tuple[str, "UsageInfo | None"]:
    provider = get_provider(agent)
    if provider == "anthropic":
        return _call_anthropic_vision(system, text, images, max_tokens)
    if provider in OPENAI_COMPATIBLE_PROVIDERS:
        return _call_openai_compatible_vision(system, text, images, max_tokens, agent)
    parts: list[str] = []
    usage: UsageInfo | None = None
    for item in stream_text(system, text, max_tokens=max_tokens, agent=agent):
        if isinstance(item, UsageInfo):
            usage = item
        else:
            parts.append(item)
    return "".join(parts), usage


def _call_openai_compatible_vision(
    system: str, text: str, images: list[dict], max_tokens: int, agent: str
) -> tuple[str, "UsageInfo | None"]:
    from openai import OpenAI

    provider = get_provider(agent)
    cfg = OPENAI_COMPATIBLE_PROVIDERS[provider]
    model = get_model_name(agent)
    client = OpenAI(
        base_url=cfg["base_url"] or None,
        api_key=resolve_provider_api_key(provider),
    )
    content: list[dict] = [{"type": "text", "text": text}]
    for img in images:
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{img['media_type']};base64,{img['base64']}"
                },
            }
        )
    messages: list[Any] = [
        {"role": "system", "content": system},
        {"role": "user", "content": content},
    ]
    resp: Any = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=messages,
    )
    text_out = resp.choices[0].message.content or ""
    u = getattr(resp, "usage", None)
    usage = (
        UsageInfo(
            input_tokens=getattr(u, "prompt_tokens", 0) or 0,
            output_tokens=getattr(u, "completion_tokens", 0) or 0,
            model=model,
        )
        if u
        else None
    )
    return text_out, usage


def _call_anthropic_vision(
    system: str, text: str, images: list[dict], max_tokens: int
) -> tuple[str, "UsageInfo | None"]:
    import anthropic

    client = anthropic.Anthropic(api_key=resolve_provider_api_key("anthropic") or None)
    content: list[dict] = []
    for img in images:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img["media_type"],
                    "data": img["base64"],
                },
            }
        )
    content.append({"type": "text", "text": text})
    messages: list[Any] = [{"role": "user", "content": content}]
    msg: Any = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    )
    text_out = "".join(b.text for b in msg.content if hasattr(b, "text"))
    return text_out, UsageInfo(
        input_tokens=msg.usage.input_tokens,
        output_tokens=msg.usage.output_tokens,
        model=ANTHROPIC_MODEL,
    )
