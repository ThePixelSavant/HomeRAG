"""Claude Code session transcripts (JSONL).

Measured across this machine's 11 transcripts: 442 tool_use blocks, 441
tool_result, 258 thinking -- and only 207 actual text blocks (55 user, 152
assistant), with Bash alone accounting for 258 tool calls and Edit 134.

Indexing that verbatim produces a search index that is roughly 80% shell
commands and file diffs, which buries the conversation it exists to recall. So
the machinery is dropped, tool calls are collapsed to a single searchable line,
and tool results are discarded except for errors -- "what broke" being the part
anyone actually searches for later.

Transcripts are VAULT tier: they carry pasted secrets, file contents and
working paths.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from app.pipeline.chunker import TextBlock
from app.pipeline.extract.base import BLOCKS, EMPTY, OK, RawDoc

logger = logging.getLogger(__name__)

# Bookkeeping records with no conversational content. `attachment` is the
# largest single category on this box (377 records) and is entirely
# environment-snapshot noise.
DROP_TYPES = {
    "attachment",
    "queue-operation",
    "atis-latch",
    "last-prompt",
    "cost-state",
    "file-history-snapshot",
    "file-history-delta",
    "bridge-session",
    "mode",
    "system",
}

TOOL_CALL_CHARS = 200
TOOL_ERROR_CHARS = 500

# A session being written to right now has a torn final line, and indexing it
# mid-turn produces a chunk that is wrong within seconds.
MIN_AGE_SECONDS = 300

# Preferred fields to summarise a tool call by, in order.
_TOOL_SUMMARY_KEYS = ("command", "file_path", "pattern", "query", "url", "path", "prompt")


def decode_project_path(dirname: str) -> str:
    """'-home-jnovick-Dev-LLM' -> '/home/jnovick/Dev/LLM'."""
    return "/" + dirname.lstrip("-").replace("-", "/") if dirname.startswith("-") else dirname


def _summarise_tool_use(block: dict) -> str:
    name = block.get("name", "tool")
    payload = block.get("input") or {}
    detail = ""
    if isinstance(payload, dict):
        for key in _TOOL_SUMMARY_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                detail = value.strip()
                break
        else:
            detail = ", ".join(sorted(payload)[:5])
    return f"[{name}] {detail[:TOOL_CALL_CHARS]}".strip()


def _result_is_error(block: dict) -> bool:
    return bool(block.get("is_error"))


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return ""


class TranscriptExtractor:
    name = "cc-transcript"

    def __init__(self, include_thinking: bool = False, include_sidechains: bool = False):
        self.include_thinking = include_thinking
        self.include_sidechains = include_sidechains

    def matches(self, path: Path) -> bool:
        return path.suffix.lower() == ".jsonl"

    def extract(self, path: Path) -> RawDoc:
        stat = path.stat()
        doc = RawDoc(
            rel_uri=path.name,
            strategy=BLOCKS,
            mtime=stat.st_mtime,
            size_bytes=stat.st_size,
            extractor=self.name,
        )

        if time.time() - stat.st_mtime < MIN_AGE_SECONDS:
            doc.status = EMPTY
            doc.status_detail = "session active within the last 5 minutes; skipped"
            return doc

        title: str | None = None
        blocks: list[TextBlock] = []
        turn_uuids: list[str] = []
        meta: dict = {}
        first_ts = last_ts = None

        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # A torn final line is normal on a session that was still being
                # appended to when it was copied. Skip it rather than failing
                # the whole document.
                continue
            if not isinstance(record, dict):
                continue

            record_type = record.get("type")

            if record_type == "ai-title":
                # Several are emitted per session as the title is refined; the
                # last one is the good one.
                title = record.get("aiTitle") or title
                continue
            if record_type in DROP_TYPES:
                continue
            if record.get("isMeta"):
                continue
            if record.get("isSidechain") and not self.include_sidechains:
                continue

            timestamp = record.get("timestamp")
            if timestamp:
                first_ts = first_ts or timestamp
                last_ts = timestamp
            for key, field in (
                ("session_id", "sessionId"),
                ("cwd", "cwd"),
                ("git_branch", "gitBranch"),
                ("cc_version", "version"),
            ):
                if record.get(field) and key not in meta:
                    meta[key] = record[field]

            message = record.get("message")
            if not isinstance(message, dict):
                continue
            role = message.get("role", record_type)
            content = message.get("content")

            if isinstance(content, str):
                text = content.strip()
                if text:
                    blocks.append(TextBlock(text=f"{role}: {text}", kind=role))
                    if record.get("uuid"):
                        turn_uuids.append(record["uuid"])
                continue
            if not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")

                if kind == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        blocks.append(TextBlock(text=f"{role}: {text}", kind=role))
                        if record.get("uuid"):
                            turn_uuids.append(record["uuid"])
                elif kind == "thinking":
                    if self.include_thinking:
                        text = (block.get("thinking") or "").strip()
                        if text:
                            blocks.append(TextBlock(text=f"thinking: {text}", kind="thinking"))
                elif kind == "tool_use":
                    summary = _summarise_tool_use(block)
                    if summary:
                        blocks.append(TextBlock(text=summary, kind="tool_use"))
                elif kind == "tool_result":
                    # Successful results are bulk. Errors are the part worth
                    # being able to find again.
                    if _result_is_error(block):
                        text = _result_text(block).strip()[:TOOL_ERROR_CHARS]
                        if text:
                            blocks.append(TextBlock(text=f"error: {text}", kind="tool_error"))

        if not blocks:
            doc.status = EMPTY
            doc.status_detail = "no conversational content after filtering"
            return doc

        doc.blocks = blocks
        doc.status = OK
        doc.title = title or meta.get("session_id") or path.stem
        doc.extra = {
            **meta,
            "project_path": decode_project_path(path.parent.name),
            "first_ts": first_ts,
            "last_ts": last_ts,
            "turn_uuids": turn_uuids[:200],
            "block_count": len(blocks),
        }
        return doc
