"""Sensitivity scanning -- the detective control behind declarative tiering.

A document's tier comes from the domain it was filed under, never from its
contents. This module does not classify; it second-guesses the filing, because
misfiling is what actually happens in practice.

It runs on OPEN-tier documents only, after extraction and before embedding. A
hit means quarantine, not index. The ordering matters: embedding writes a
plaintext vector to Qdrant, and inversion recovers most of the source text from
a vector, so there is no "index it and clean up later".

This is a net with holes. It catches formatted identifiers -- card numbers,
keys, account shapes -- not "my bank password is hunter2" written in prose.
Declarative placement remains the real control.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_CARD_RE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
_ROUTING_RE = re.compile(r"\b(?:routing|aba)\s*(?:number|no\.?|#)?\s*[:=]?\s*\d{9}\b", re.I)
_ACCOUNT_RE = re.compile(
    r"\b(?:account|acct)\s*(?:number|no\.?|#)\s*[:=]?\s*[\dX*-]{6,}\b", re.I
)

_SECRET_RES: list[tuple[str, re.Pattern[str]]] = [
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("assigned_secret", re.compile(r"\b(?:password|passwd|secret|api_?key|token)\s*[:=]\s*\S{8,}", re.I)),
]

# Individually unremarkable, collectively a statement or invoice.
_FINANCIAL_GROUPS: list[tuple[str, set[str]]] = [
    ("bank_statement", {"statement", "balance", "account"}),
    ("invoice", {"invoice", "total", "due"}),
    ("card_statement", {"payment", "minimum", "credit limit"}),
]
_FINANCIAL_MIN_HITS = 3


@dataclass
class Finding:
    rule: str
    detail: str


@dataclass
class ScanResult:
    findings: list[Finding]

    @property
    def sensitive(self) -> bool:
        return bool(self.findings)

    def reason(self) -> str:
        return "; ".join(f"{f.rule}: {f.detail}" for f in self.findings)


def luhn_valid(digits: str) -> bool:
    """Checksum used by real card numbers.

    Without it, every order number and long identifier trips the scanner and
    the quarantine queue becomes noise people learn to wave through.
    """
    nums = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(nums) <= 19:
        return False
    total, parity = 0, len(nums) % 2
    for i, n in enumerate(nums):
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _redact(match: str) -> str:
    """Never echo a live secret into a log or a review queue."""
    stripped = match.strip()
    if len(stripped) <= 8:
        return "*" * len(stripped)
    return f"{stripped[:4]}...{stripped[-2:]} ({len(stripped)} chars)"


def scan(text: str) -> ScanResult:
    findings: list[Finding] = []

    for candidate in _CARD_RE.findall(text):
        digits = re.sub(r"[ -]", "", candidate)
        if luhn_valid(digits):
            findings.append(Finding("card_number", f"Luhn-valid, {len(digits)} digits"))
            break

    for rule, regex in (
        ("ssn", _SSN_RE),
        ("iban", _IBAN_RE),
        ("routing_number", _ROUTING_RE),
        ("account_number", _ACCOUNT_RE),
    ):
        match = regex.search(text)
        if match:
            findings.append(Finding(rule, _redact(match.group(0))))

    for rule, regex in _SECRET_RES:
        match = regex.search(text)
        if match:
            findings.append(Finding(rule, _redact(match.group(0))))

    lowered = text.lower()
    for rule, terms in _FINANCIAL_GROUPS:
        hits = {t for t in terms if t in lowered}
        if len(hits) >= _FINANCIAL_MIN_HITS:
            findings.append(Finding(rule, f"co-occurring terms: {', '.join(sorted(hits))}"))

    return ScanResult(findings)


# Redaction applied to vault-tier text before embedding. Defence in depth: the
# vault is encrypted at rest anyway, but a secret that never enters a vector is
# one fewer thing to reason about.
_REDACTIONS: list[tuple[str, re.Pattern[str]]] = [
    ("key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}")),
    (
        "block",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    ),
    ("secret", re.compile(r"\b((?:password|passwd|secret|api_?key|token)\s*[:=]\s*)\S{8,}", re.I)),
]


def redact(text: str) -> str:
    for label, regex in _REDACTIONS:
        if regex.groups:
            text = regex.sub(lambda m: f"{m.group(1)}[REDACTED:{label}]", text)
        else:
            text = regex.sub(f"[REDACTED:{label}]", text)
    return text
