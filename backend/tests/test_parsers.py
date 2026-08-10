"""Tests for backend.app.services.parsers.

Requires no ANTHROPIC_API_KEY and no live Postgres: every parser reads
from a local fixture under backend/tests/fixtures/parsers/.
"""

from pathlib import Path

import pytest

from backend.app.services import parsers
from backend.app.services.parsers import ParsedDocument, UnsupportedFormat, parse_file

FIXTURES = Path(__file__).parent / "fixtures" / "parsers"


def _assert_spans_are_valid(doc: ParsedDocument) -> None:
    assert doc.segments, "expected at least one segment"
    for segment in doc.segments:
        assert 0 <= segment.char_start <= segment.char_end <= len(doc.text)


async def test_parse_md_segments_by_top_level_heading():
    doc = await parse_file(FIXTURES / "sample.md")

    assert doc.mime_type == "text/markdown"
    assert doc.text.strip()
    assert len(doc.segments) == 2  # "# Overview" and "# Requirements"
    _assert_spans_are_valid(doc)
    assert doc.text[doc.segments[0].char_start : doc.segments[0].char_end].startswith("# Overview")
    assert doc.text[doc.segments[1].char_start : doc.segments[1].char_end].startswith(
        "# Requirements"
    )


async def test_parse_txt_falls_back_to_whole_doc_segment():
    doc = await parse_file(FIXTURES / "sample.txt")

    assert doc.mime_type == "text/plain"
    assert doc.text.strip()
    assert len(doc.segments) == 1
    assert doc.segments[0].char_start == 0
    assert doc.segments[0].char_end == len(doc.text)
    _assert_spans_are_valid(doc)


async def test_parse_pdf_extracts_text_per_page():
    doc = await parse_file(FIXTURES / "sample.pdf")

    assert doc.mime_type == "application/pdf"
    assert doc.text.strip()
    assert len(doc.segments) == 2
    assert [s.page for s in doc.segments] == [1, 2]
    _assert_spans_are_valid(doc)
    for segment in doc.segments:
        assert doc.text[segment.char_start : segment.char_end].strip()


async def test_parse_pdf_falls_back_to_pdfplumber_for_sparse_pages(monkeypatch):
    """A page pypdf extracts under 40 chars of text from should be retried
    with pdfplumber, and the richer text should win."""

    class _FakePage:
        def extract_text(self):
            return "short"

    class _FakeReader:
        def __init__(self, _path):
            self.pages = [_FakePage()]

    class _FakePlumberPage:
        def extract_text(self):
            return "this is the much longer text pdfplumber recovered from the page"

    class _FakePlumberDoc:
        def __init__(self, _path):
            self.pages = [_FakePlumberPage()]

        def close(self):
            pass

    monkeypatch.setattr(parsers, "PdfReader", _FakeReader)
    monkeypatch.setattr(parsers.pdfplumber, "open", _FakePlumberDoc)

    doc = parsers._parse_pdf(FIXTURES / "sample.pdf", "application/pdf")

    assert doc.text == "this is the much longer text pdfplumber recovered from the page"
    assert doc.segments == [parsers.Segment(char_start=0, char_end=len(doc.text), page=1)]


async def test_parse_docx_walks_paragraphs():
    doc = await parse_file(FIXTURES / "sample.docx")

    assert (
        doc.mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert doc.text.strip()
    assert len(doc.segments) == 3  # blank paragraph is skipped
    _assert_spans_are_valid(doc)
    assert doc.text[doc.segments[0].char_start : doc.segments[0].char_end] == "Export Feature PRD"


async def test_parse_csv_renders_header_prefixed_rows():
    doc = await parse_file(FIXTURES / "sample.csv")

    assert doc.mime_type == "text/csv"
    assert doc.text.strip()
    assert len(doc.segments) == 3  # 3 data rows, header consumed as the prefix
    _assert_spans_are_valid(doc)
    first_row = doc.text[doc.segments[0].char_start : doc.segments[0].char_end]
    assert (
        first_row
        == "id: TICKET-101\ntitle: Add CSV export button\nstatus: in_progress\nowner: priya"
    )


async def test_parse_json_emits_one_segment_per_array_item_with_jsonpath():
    doc = await parse_file(FIXTURES / "sample.json")

    assert doc.mime_type == "application/json"
    assert doc.text.strip()
    assert len(doc.segments) == 2
    _assert_spans_are_valid(doc)
    first = doc.text[doc.segments[0].char_start : doc.segments[0].char_end]
    assert first.startswith("$[0]\n")
    assert "TICKET-101" in first
    second = doc.text[doc.segments[1].char_start : doc.segments[1].char_end]
    assert second.startswith("$[1]\n")


async def test_parse_json_object_uses_key_as_jsonpath(tmp_path):
    path = tmp_path / "sample.json"
    path.write_text('{"a": {"x": 1}, "b": {"y": 2}}', encoding="utf-8")

    doc = await parse_file(path)

    assert len(doc.segments) == 2
    first = doc.text[doc.segments[0].char_start : doc.segments[0].char_end]
    assert first.startswith("$.a\n")


async def test_unsupported_extension_is_rejected(tmp_path):
    path = tmp_path / "sample.exe"
    path.write_bytes(b"MZ\x90\x00\x03\x00\x00\x00")

    with pytest.raises(UnsupportedFormat):
        await parse_file(path)


async def test_content_mismatching_extension_is_rejected(tmp_path):
    path = tmp_path / "fake.pdf"
    path.write_text("this is not actually a pdf, just text", encoding="utf-8")

    with pytest.raises(UnsupportedFormat):
        await parse_file(path)
