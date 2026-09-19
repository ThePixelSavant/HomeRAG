"""The domain -> tier registry.

Classification is declarative: a document's tier is decided by the domain it is
filed under, never inferred from its contents. An unknown domain is an error
rather than a default, because silently defaulting a typo to the open tier
writes a plaintext vector to Qdrant, and a vector is recoverable back to its
source text by inversion.
"""

from __future__ import annotations

import uuid
from enum import Enum

# Namespace for deterministic chunk point IDs. Frozen: changing it re-IDs every
# point in the index and orphans everything already stored.
POINT_NAMESPACE = uuid.UUID("6f1a9c2e-5b74-4e8a-9d31-0c7f2ab48e15")

# Bumped when the payload schema changes in a way that makes existing points
# unreadable. Stored in the collection fingerprint and checked at startup.
SCHEMA_VERSION = 1


class Tier(str, Enum):
    OPEN = "open"
    VAULT = "vault"


DOMAIN_TIERS: dict[str, Tier] = {
    # Open tier: Qdrant, plaintext, searchable by any local model.
    "manuals": Tier.OPEN,
    "sdk-docs": Tier.OPEN,
    "notes": Tier.OPEN,
    "infra": Tier.OPEN,
    # Vault tier: SQLCipher, sealed by default, three gates on every read.
    "receipts": Tier.VAULT,
    "financial": Tier.VAULT,
    "transcripts": Tier.VAULT,
    "personal": Tier.VAULT,
}


class UnknownDomainError(ValueError):
    """Raised when a domain is not in the registry.

    Deliberately fatal. The alternative -- defaulting -- routes misfiled
    sensitive data into the open tier.
    """

    def __init__(self, domain: str) -> None:
        super().__init__(
            f"Unknown domain {domain!r}. Known domains: {', '.join(sorted(DOMAIN_TIERS))}. "
            "Add it to DOMAIN_TIERS with an explicit tier before ingesting."
        )
        self.domain = domain


def tier_of(domain: str) -> Tier:
    try:
        return DOMAIN_TIERS[domain]
    except KeyError:
        raise UnknownDomainError(domain) from None


def is_vault(domain: str) -> bool:
    return tier_of(domain) is Tier.VAULT


def domains_in(tier: Tier) -> list[str]:
    return sorted(d for d, t in DOMAIN_TIERS.items() if t is tier)


def open_domains() -> list[str]:
    return domains_in(Tier.OPEN)


def vault_domains() -> list[str]:
    return domains_in(Tier.VAULT)
