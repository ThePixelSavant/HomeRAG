"""The one client for llama-server's chat endpoint, used to read receipts.

Runs in the keyless parse worker (see app/pipeline/parse_worker.py), so it
must not import anything key-bearing; the boundary test enforces that.

The answer is constrained by a JSON schema, which llama.cpp compiles into a
grammar: the model cannot return anything that fails to parse, so a malformed
reading is impossible rather than merely unlikely. What it CAN return is a
wrong reading, which is why the parent validates every field it gets.
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


def read_json(*, prompt: str, schema: dict, text: str | None = None, images: list[bytes] = ()) -> dict:
    """Ask the vision model one question and return its JSON answer.

    `images` are JPEG bytes. `text` is a document's own text layer, sent
    instead of an image when there is one -- far cheaper to prefill.
    """
    content: list[dict] = [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(img).decode()}}
        for img in images
    ]
    if text:
        content.append({"type": "text", "text": text})
    body = {
        "model": settings.vision_model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": content},
        ],
        "response_format": {"type": "json_schema", "json_schema": {"name": "reading", "schema": schema}},
    }
    url = f"{settings.llama_url.rstrip('/')}/v1/chat/completions"
    try:
        response = httpx.post(url, json=body, timeout=settings.vision_timeout_seconds)
        response.raise_for_status()
        answer = response.json()["choices"][0]["message"]["content"]
        return json.loads(answer)
    except httpx.HTTPStatusError as exc:
        raise VisionError(
            f"{url} refused the request ({exc.response.status_code}): {exc.response.text[:200]}"
        ) from None
    except httpx.HTTPError as exc:
        raise VisionError(f"cannot reach {url} for model {settings.vision_model!r}: {exc}") from None
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise VisionError(f"unexpected answer from {settings.vision_model!r}: {exc}") from None
