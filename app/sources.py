"""Source manifest parsing.

Every source must declare a `domain`, and that domain must be in the registry.
There is no default: defaulting a typo would route a source into whichever tier
happened to be first, and in the open direction that is unrecoverable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.config import settings
from app.domains import Tier, UnknownDomainError, tier_of

LOCAL = "local"
INBOX = "inbox"
TRANSCRIPTS = "transcripts"
GIT = "git"
WEB = "web"

PHASE_1_TYPES = {LOCAL, INBOX, TRANSCRIPTS}
KNOWN_TYPES = PHASE_1_TYPES | {GIT, WEB}


class SourceConfigError(ValueError):
    pass


@dataclass
class Source:
    id: str
    type: str
    domain: str
    path: Path | None = None
    url: str | None = None
    branch: str | None = None
    cadence: str | None = None
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    enabled: bool = True

    @property
    def tier(self) -> Tier:
        return tier_of(self.domain)

    @property
    def is_vault(self) -> bool:
        return self.tier is Tier.VAULT


def _parse_one(raw: dict, index: int) -> Source:
    where = raw.get("id") or f"entry #{index}"

    for required in ("id", "type", "domain"):
        if not raw.get(required):
            raise SourceConfigError(
                f"Source {where}: missing required key {required!r}. "
                "`domain` in particular is mandatory -- it decides whether the "
                "source is stored in plaintext or encrypted."
            )

    source_type = raw["type"]
    if source_type not in KNOWN_TYPES:
        raise SourceConfigError(
            f"Source {where}: unknown type {source_type!r}. Known: {', '.join(sorted(KNOWN_TYPES))}"
        )
    if source_type not in PHASE_1_TYPES:
        raise SourceConfigError(
            f"Source {where}: type {source_type!r} is not implemented yet (Phase 2). "
            "Set `enabled: false` or remove it."
        )

    try:
        tier_of(raw["domain"])
    except UnknownDomainError as exc:
        raise SourceConfigError(f"Source {where}: {exc}") from None

    if source_type in {LOCAL, TRANSCRIPTS} and not raw.get("path"):
        raise SourceConfigError(f"Source {where}: type {source_type!r} requires `path`.")

    return Source(
        id=raw["id"],
        type=source_type,
        domain=raw["domain"],
        path=Path(raw["path"]) if raw.get("path") else None,
        url=raw.get("url"),
        branch=raw.get("branch"),
        cadence=raw.get("cadence"),
        include=list(raw.get("include") or []),
        exclude=list(raw.get("exclude") or []),
        enabled=bool(raw.get("enabled", True)),
    )


def load_sources(path: Path | None = None) -> list[Source]:
    target = path or settings.sources_file
    if not target.exists():
        return []
    data = yaml.safe_load(target.read_text()) or {}
    entries = data.get("sources") or []
    if not isinstance(entries, list):
        raise SourceConfigError("`sources` must be a list.")

    sources = [_parse_one(raw, i) for i, raw in enumerate(entries)]

    seen: set[str] = set()
    for source in sources:
        if source.id in seen:
            raise SourceConfigError(f"Duplicate source id {source.id!r}.")
        seen.add(source.id)
    return sources


def inbox_sources(root: Path | None = None) -> list[Source]:
    """One implicit source per inbox subdirectory.

    The directory name IS the domain -- that is the whole intake UX. Dropping a
    file into inbox/manuals/ files it as `manuals` with no config edit, and an
    unrecognised directory name raises rather than being quietly indexed.
    """
    base = root or settings.inbox_path
    if not base.exists():
        return []

    sources = []
    for child in sorted(base.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        try:
            tier_of(child.name)
        except UnknownDomainError as exc:
            raise SourceConfigError(
                f"Inbox directory {child}/ does not name a known domain. {exc}"
            ) from None
        sources.append(
            Source(id=f"inbox:{child.name}", type=INBOX, domain=child.name, path=child)
        )
    return sources


def all_sources(sources_file: Path | None = None, inbox_root: Path | None = None) -> list[Source]:
    return [s for s in (*inbox_sources(inbox_root), *load_sources(sources_file)) if s.enabled]


def find(source_id: str, **kwargs) -> Source | None:
    for source in all_sources(**kwargs):
        if source.id == source_id:
            return source
    return None
