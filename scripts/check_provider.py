"""Explicit, synthetic provider diagnostics; never a database or production tool.

Only allowlisted protocol fields and numeric counters leave this process. Successful
samples cannot prove a provider's hard token limit, billing, or server cancellation.
"""

import argparse
import asyncio
import hashlib
import json
import time
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Literal

from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from langsmith import tracing_context
from openai import APITimeoutError, DefaultAsyncHttpxClient, DefaultHttpxClient
from pydantic import BaseModel, ConfigDict, ValidationError

from db_agent.config import ConfigurationError, Settings, load_settings


class ProviderProbe(BaseModel):
    """Return the fixed synthetic values requested by the protocol diagnostic."""

    model_config = ConfigDict(extra="forbid", strict=True)
    value: Literal[7]
    marker: Literal["provider-probe-v1"]


def _counter(value):
    return value if type(value) is int and value >= 0 else None


def _usage(message, requested_tokens):
    normalized = message.usage_metadata or {}
    provider = message.response_metadata.get("token_usage") or {}
    if not isinstance(provider, dict):
        provider = {}
    normalized_counts = {
        key: _counter(normalized.get(key))
        for key in ("input_tokens", "output_tokens", "total_tokens")
    }
    provider_counts = {
        key: _counter(provider.get(key))
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    details = provider.get("completion_tokens_details")
    provider_counts["reasoning_tokens"] = (
        _counter(details.get("reasoning_tokens")) if isinstance(details, dict) else None
    )
    output_counts = [
        value for value in (
            normalized_counts["output_tokens"], provider_counts["completion_tokens"],
        ) if value is not None
    ]
    return {
        "normalized": normalized_counts,
        "provider": provider_counts,
        "output_counters_agree": (
            output_counts[0] == output_counts[1] if len(output_counts) == 2 else None
        ),
        "reported_output_exceeds_requested": (
            any(value > requested_tokens for value in output_counts) if output_counts else None
        ),
    }


def _structured_valid(response):
    if not isinstance(response, dict) or response.get("parsing_error") is not None:
        return False
    raw, parsed = response.get("raw"), response.get("parsed")
    if (
        "parsing_error" not in response or not isinstance(raw, AIMessage)
        or not isinstance(parsed, ProviderProbe) or raw.invalid_tool_calls
        or len(raw.tool_calls) != 1
        or raw.response_metadata.get("finish_reason") not in {"stop", "tool_calls"}
        or raw.text.strip()
    ):
        return False
    try:
        # include_raw returns the framework AIMessage, not the original HTTP body.
        # Current LangChain stores its original parsed call in raw.tool_calls.
        call = raw.tool_calls[0]
        if call["name"] != ProviderProbe.__name__ or not call["id"]:
            return False
        return (
            ProviderProbe.model_validate(call["args"])
            == ProviderProbe.model_validate(parsed)
        )
    except (KeyError, TypeError, ValueError, ValidationError):
        return False


def _observation(response, structured, token_budget):
    raw = response.get("raw") if structured and isinstance(response, dict) else response
    if not isinstance(raw, AIMessage):
        return {"outcome": "protocol_error", "protocol_valid": False}
    finish = raw.response_metadata.get("finish_reason")
    valid = _structured_valid(response) if structured else (
        bool(raw.text.strip()) and not raw.tool_calls and not raw.invalid_tool_calls
        and finish == "stop"
    )
    return {
        "outcome": "response",
        "protocol_valid": valid,
        "finish_reason": finish if finish in {
            "stop", "length", "tool_calls", "function_call", "content_filter",
        } else "unknown",
        "truncated": finish == "length",
        "tool_call_count": len(raw.tool_calls),
        "invalid_tool_call_count": len(raw.invalid_tool_calls),
        "usage": _usage(raw, token_budget),
    }


async def run_probe(settings: Settings, mode: str = "protocol") -> dict:
    """At most two model calls; share the configured total timeout and never retry."""
    if mode not in {"protocol", "http-timeout", "total-timeout", "token-limit"}:
        raise ValueError("unsupported probe mode")
    token_budget = min(64, settings.max_output_tokens) if mode == "token-limit" else (
        settings.max_output_tokens
    )
    text_prompt = (
        "List every integer from 1 through 200, one integer per line. "
        "Do not summarize, skip any integers, or add other text."
        if mode == "token-limit" else "Reply with exactly the text: provider-probe-v1"
    )
    request_timeout = min(settings.request_timeout_seconds, 0.01) if mode == "http-timeout" else (
        settings.request_timeout_seconds
    )
    total_timeout = min(settings.run_timeout_seconds, 0.01) if mode == "total-timeout" else (
        settings.run_timeout_seconds
    )
    requests = []
    statuses = []

    async def capture_request(request):
        payload = json.loads(await request.aread())
        requests.append({
            "max_tokens": _counter(payload.get("max_tokens")),
            "max_completion_tokens": _counter(payload.get("max_completion_tokens")),
            "stream": payload.get("stream") is True,
            "tool_count": len(payload.get("tools", [])),
            "tool_choice_auto": payload.get("tool_choice") == "auto",
        })

    async def capture_response(response):
        statuses.append(response.status_code)

    report = {
        "schema_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "lock_sha256": hashlib.sha256(
            (Path(__file__).resolve().parents[1] / "uv.lock").read_bytes(),
        ).hexdigest(),
        "mode": mode,
        "structured_protocol_layer": "langchain_include_raw_ai_message",
        "endpoint_sha256": hashlib.sha256(settings.openai_base_url.encode()).hexdigest(),
        "model_sha256": hashlib.sha256(settings.model.encode()).hexdigest(),
        "versions": {name: version(name) for name in ("langchain-openai", "openai", "httpx")},
        "limits": {
            "configured_output_tokens": settings.max_output_tokens,
            "requested_output_tokens": token_budget,
            "http_timeout_seconds": request_timeout,
            "total_timeout_seconds": total_timeout,
            "max_model_calls": min(settings.max_model_calls, 2 if mode == "protocol" else 1),
            "automatic_retries": 0,
        },
        "requests": requests,
        "http_statuses": statuses,
        "observations": [],
        "hard_token_limit_verified": False,
        "billing_verified": False,
        "provider_server_cancellation_verified": False,
    }
    async with AsyncExitStack() as clients:
        clients.enter_context(tracing_context(enabled=False))
        sync_client = clients.enter_context(DefaultHttpxClient())
        async_client = await clients.enter_async_context(DefaultAsyncHttpxClient(
            event_hooks={"request": [capture_request], "response": [capture_response]},
        ))
        # Match run_agent's explicit configuration; no global OPENAI_* credentials.
        model = ChatOpenAI(
            model=settings.model, api_key=settings.api_key, base_url=settings.openai_base_url,
            timeout=request_timeout, max_retries=0, max_tokens=token_budget,
            streaming=False, use_responses_api=False,
            http_client=sync_client, http_async_client=async_client,
        )
        deadline = time.monotonic() + total_timeout
        for phase in (["text", "function_calling"] if mode == "protocol" else ["text"]):
            if len(report["observations"]) >= report["limits"]["max_model_calls"]:
                report["observations"].append({"phase": phase, "outcome": "model_call_limit"})
                break
            started = time.monotonic()
            structured = phase == "function_calling"
            try:
                async with asyncio.timeout(max(0, deadline - started)):
                    runnable = model.with_structured_output(
                        ProviderProbe, method="function_calling", include_raw=True,
                        tool_choice="auto",
                    ) if structured else model
                    response = await runnable.ainvoke([
                        ("system", "This is a synthetic protocol diagnostic. Follow the request."),
                        ("human", "Call ProviderProbe exactly once with value 7 and marker "
                         "provider-probe-v1. Return no other content." if structured
                         else text_prompt),
                    ])
                    observation = _observation(response, structured, token_budget)
            except APITimeoutError:
                observation = {"outcome": "http_timeout"}
            except TimeoutError:
                observation = {"outcome": "total_timeout"}
            except Exception:
                # Provider errors may contain credentials, URL paths or raw response text.
                observation = {"outcome": "request_error"}
            observation.update(phase=phase, duration_ms=round((time.monotonic() - started) * 1000))
            report["observations"].append(observation)
            if observation["outcome"] == "total_timeout":
                break
    report["clients_closed"] = sync_client.is_closed and async_client.is_closed
    if mode == "protocol":
        report["protocol_compatible"] = len(report["observations"]) == 2 and all(
            item.get("protocol_valid") is True for item in report["observations"]
        )
    elif mode == "token-limit":
        observation = report["observations"][0]
        # Obtaining an observation is separate from observing length or cap compliance.
        report["token_limit_response_observed"] = observation["outcome"] == "response"
        report["length_finish_observed"] = observation.get("truncated") is True
    else:
        report["expected_timeout_observed"] = report["observations"][0]["outcome"] == (
            "http_timeout" if mode == "http-timeout" else "total_timeout"
        )
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run", action="store_true", help="explicitly send synthetic model requests",
    )
    parser.add_argument(
        "--mode", choices=("protocol", "http-timeout", "total-timeout", "token-limit"),
        default="protocol",
    )
    args = parser.parse_args(argv)
    if not args.run:
        parser.error("--run is required; this command contacts the configured provider")
    try:
        report = asyncio.run(run_probe(load_settings(), args.mode))
    except ConfigurationError:
        print(json.dumps({"status": "configuration_error"}))
        return 2
    except Exception:
        print(json.dumps({"status": "probe_error"}))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    observed = report.get("protocol_compatible", report.get(
        "token_limit_response_observed", report.get("expected_timeout_observed"),
    ))
    return 0 if observed else 1


if __name__ == "__main__":
    raise SystemExit(main())
