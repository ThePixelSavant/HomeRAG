"""Pipeline invariants: chunking, identity, classification, tiering."""

from __future__ import annotations

import pytest

from app.config import settings
from app.domains import Tier, UnknownDomainError, tier_of
from app.pipeline import chunker, classify
from app.pipeline.chunker import TextBlock
from app.pipeline.extract.base import doc_id_for
from app.pipeline.qdrant_store import point_id

# Long enough to exceed the model ceiling, so the chunking tests exercise more
# than a single chunk and the tokenizer test proves truncation is disabled.
LOREM = "The pump must be primed before first use. " * 200


# --- chunking --------------------------------------------------------------


def test_every_chunk_fits_the_model_ceiling():
    """Over-budget text is truncated by the model with no error, so the
    chunker must never emit any."""
    for chunk in chunker.chunk_text(LOREM):
        assert chunk.token_count <= settings.chunk_max_tokens


def test_chunk_indices_are_contiguous():
    chunks = chunker.chunk_text(LOREM)
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_empty_text_yields_nothing():
    assert chunker.chunk_text("") == []
    assert chunker.chunk_text("   \n  ") == []


def test_markdown_carries_the_heading_breadcrumb():
    md = "# Pump\n\n## Wiring\n\nTighten to 18 ft-lb in a star pattern.\n"
    chunk = chunker.chunk_markdown(md)[0]
    assert chunk.heading_path == ["Pump", "Wiring"]
    # The breadcrumb must be in the embedded text, not only in metadata:
    # a torque spec is meaningless without knowing the assembly.
    assert chunk.text.startswith("Pump > Wiring")


def test_blocks_are_not_split_when_they_fit():
    blocks = [TextBlock(text=f"Turn {i}. " + "word " * 20, kind="user") for i in range(4)]
    chunks = chunker.chunk_blocks(blocks)
    assert len(chunks) == 1
    for i in range(4):
        assert f"Turn {i}." in chunks[0].text


def test_oversized_block_is_split_and_still_fits():
    chunks = chunker.chunk_blocks([TextBlock(text="alpha beta gamma " * 500, kind="tool")])
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.token_count <= settings.chunk_max_tokens


def test_long_text_is_not_measured_through_a_truncating_tokenizer():
    """fastembed's own tokenizer truncates at the model max; counting with it
    would cap every measurement at 512 and hide the rest of the document."""
    from app.pipeline.embedder import MODEL_MAX_TOKENS, count_tokens

    assert count_tokens(LOREM) > MODEL_MAX_TOKENS


# --- identity --------------------------------------------------------------


def test_point_ids_are_deterministic():
    assert point_id("s", "d", 3) == point_id("s", "d", 3)


def test_point_ids_differ_by_position_and_document():
    assert point_id("s", "d", 3) != point_id("s", "d", 4)
    assert point_id("s", "d", 3) != point_id("s", "e", 3)


def test_doc_id_uses_a_source_relative_uri():
    """Hashing an absolute container path would orphan the whole index the
    first time a bind mount changed."""
    assert doc_id_for("src", "a/b.md") == doc_id_for("src", "a/b.md")
    assert doc_id_for("src", "a/b.md") != doc_id_for("other", "a/b.md")


# --- classification --------------------------------------------------------


def test_luhn_valid_card_is_flagged():
    assert classify.scan("paid with 4539 1488 0343 6467 today").sensitive


def test_non_luhn_long_number_is_not_flagged():
    """Order numbers must not flood the review queue, or it becomes noise
    people learn to wave through."""
    assert not classify.scan("order number 1234 5678 9012 3456").sensitive


@pytest.mark.parametrize(
    "text",
    [
        "SSN: 123-45-6789",
        "ANTHROPIC_API_KEY=sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWx",
        "token ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
        "Statement. Opening balance $1,204. Account ending 4432.",
    ],
)
def test_sensitive_shapes_are_flagged(text):
    assert classify.scan(text).sensitive


def test_ordinary_prose_is_not_flagged():
    assert not classify.scan("I should budget better for tools this year.").sensitive


def test_findings_never_echo_the_live_secret():
    reason = classify.scan("key sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWx").reason()
    assert "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWx" not in reason


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWx",
        "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123",
        "AKIAIOSFODNN7EXAMPLE",
    ],
)
def test_redaction_removes_secrets_before_embedding(secret):
    assert secret not in classify.redact(f"here it is: {secret} end")


# --- tiering ---------------------------------------------------------------


def test_known_domains_map_to_expected_tiers():
    assert tier_of("manuals") is Tier.OPEN
    assert tier_of("receipts") is Tier.VAULT
    assert tier_of("transcripts") is Tier.VAULT


def test_unknown_domain_raises_rather_than_defaulting():
    """Defaulting a typo would route sensitive data into the open tier, and a
    plaintext vector cannot be un-published."""
    with pytest.raises(UnknownDomainError):
        tier_of("reciepts")


# --- source manifest -------------------------------------------------------


def test_source_without_domain_is_rejected(tmp_path):
    import yaml

    from app.sources import SourceConfigError, load_sources

    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump({"sources": [{"id": "x", "type": "local", "path": "/docs"}]}))
    with pytest.raises(SourceConfigError, match="domain"):
        load_sources(path)


def test_inbox_directory_must_name_a_known_domain(tmp_path):
    from app.sources import SourceConfigError, inbox_sources

    (tmp_path / "reciepts").mkdir()
    with pytest.raises(SourceConfigError):
        inbox_sources(tmp_path)


def test_inbox_directory_name_sets_the_domain(tmp_path):
    from app.sources import inbox_sources

    (tmp_path / "receipts").mkdir()
    (tmp_path / "manuals").mkdir()
    found = {s.domain: s for s in inbox_sources(tmp_path)}
    assert found["receipts"].is_vault
    assert not found["manuals"].is_vault


# --- enumeration -----------------------------------------------------------


def test_files_directly_in_a_source_root_are_found(tmp_path):
    """Regression: fnmatch("a.md", "**/*") is False because "**/" needs a
    literal slash, which made every inbox drop invisible."""
    from app.pipeline.sync import enumerate_files
    from app.sources import Source

    (tmp_path / "pump.md").write_text("# Pump\n")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "other.md").write_text("# Other\n")

    files, complete = enumerate_files(
        Source(id="s", type="inbox", domain="manuals", path=tmp_path)
    )
    assert complete
    assert {f.name for f in files} == {"pump.md", "other.md"}


def test_missing_source_path_reports_incomplete_enumeration(tmp_path):
    """The sweep keys off this flag; an empty result from a broken mount is
    indistinguishable from a source that genuinely lost every file."""
    from app.pipeline.sync import enumerate_files
    from app.sources import Source

    files, complete = enumerate_files(
        Source(id="s", type="local", domain="notes", path=tmp_path / "nope")
    )
    assert files == []
    assert complete is False
