import pytest

from moppu.ingestion.transcript import chunk_text


def test_chunk_text_basic():
    text = "a" * 5000
    chunks = chunk_text(text, chunk_size=1000, overlap=100)
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(chunks[:1])[:1000] == text[:1000]
    # Overlap means total reconstructed length > input length
    assert sum(len(c) for c in chunks) > len(text)


def test_chunk_text_rejects_bad_overlap():
    with pytest.raises(ValueError):
        chunk_text("abc", chunk_size=10, overlap=10)
