"""The one client for llama-server's chat endpoint: receipts and PDF pages.

Runs in the keyless parse worker (see app/pipeline/parse_worker.py), so it
must not import anything key-bearing; the boundary test enforces that.

A receipt reading is constrained by a JSON schema, which llama.cpp compiles
into a grammar: the model cannot return anything that fails to parse, so a
malformed reading is impossible rather than merely unlikely. What it CAN
return is a wrong reading, which is why the parent validates every field it
gets. A page transcription is free markdown and is asked for as plain text
(see read_text).
"""

from __future__ import annotations

import base64
import json
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class VisionError(RuntimeError):
    """llama-server unreachable, refused the request, or answered off-schema."""


class VisionTruncated(VisionError):
    """The answer reached `max_tokens` before it finished.

    At temperature 0 that is usually the model looping -- the first real page
    transcribed ran past 6,000 tokens of a ~2,000-token page. Unlike other
    failures it is a property of the input, so retrying buys the same loop.
    """


def read_json(
    *, prompt: str, schema: dict, text: str | None = None, images: list[bytes] = (),
    max_tokens: int | None = None,
) -> dict:
    """Ask the vision model one question and return its JSON answer.

    `images` are JPEG bytes. `text` is a document's own text layer, sent
    instead of an image when there is one -- far cheaper to prefill.
    """
    answer = _ask(
        prompt, _content(text, images), max_tokens,
        response_format={"type": "json_schema", "json_schema": {"name": "reading", "schema": schema}},
    )
    try:
        return json.loads(answer)
    except json.JSONDecodeError as exc:
        raise VisionError(f"unexpected answer from {settings.vision_model!r}: {exc}") from None


def read_text(*, prompt: str, images: list[bytes], max_tokens: int | None = None) -> str:
    """Ask the vision model for free text -- a page transcribed as markdown.

    Not JSON: wrapping free-form markdown in a schema bought nothing, and on
    the first real page the model finished the transcription and then wrote
    escaped newlines inside the string until it hit the cap. Unconstrained, it
    ends the answer itself.
    """
    return _ask(prompt, _content(None, images), max_tokens)


def _content(text: str | None, images: list[bytes]) -> list[dict]:
    content: list[dict] = [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(img).decode()}}
        for img in images
    ]
    if text:
        content.append({"type": "text", "text": text})
    return content


def _ask(prompt: str, content: list[dict], max_tokens: int | None, **extra) -> str:
    """One chat completion; the answer's text.

    `max_tokens` (default `settings.vision_max_tokens`) is the only thing that
    stops a runaway answer: llama-server's router keeps generating after the
    client gives up, so the httpx timeout below frees this process and leaves
    the CPU busy for up to the whole context.
    """
    body = {
        "model": settings.vision_model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": content},
        ],
        "max_tokens": max_tokens or settings.vision_max_tokens,
        **extra,
    }
    url = f"{settings.llama_url.rstrip('/')}/v1/chat/completions"
    try:
        response = httpx.post(url, json=body, timeout=settings.vision_timeout_seconds)
        response.raise_for_status()
        choice = response.json()["choices"][0]
        answer = choice["message"]["content"]
    except httpx.HTTPStatusError as exc:
        raise VisionError(
            f"{url} refused the request ({exc.response.status_code}): {exc.response.text[:200]}"
        ) from None
    except httpx.HTTPError as exc:
        raise VisionError(f"cannot reach {url} for model {settings.vision_model!r}: {exc}") from None
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise VisionError(f"unexpected answer from {settings.vision_model!r}: {exc}") from None
    if choice.get("finish_reason") == "length":
        raise VisionTruncated(f"{settings.vision_model!r} was still answering at {body['max_tokens']} tokens")
    return answer
