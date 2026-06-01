"""Unit tests for Notion block→text conversion and chunking (network-free)."""
from app.rag import notion


def _rich(text):
    return [{"type": "text", "text": {"content": text}, "plain_text": text}]


def test_blocks_to_text_extracts_paragraphs_and_headings():
    blocks = [
        {"type": "heading_1", "heading_1": {"rich_text": _rich("红利策略")}},
        {"type": "paragraph", "paragraph": {"rich_text": _rich("高股息逻辑。")}},
        {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rich("条目一")}},
        {"type": "image", "image": {}},  # non-text block → skipped, no crash
    ]
    text = notion.blocks_to_text(blocks)
    assert "红利策略" in text
    assert "高股息逻辑。" in text
    assert "条目一" in text


def test_blocks_to_text_empty_is_empty_string():
    assert notion.blocks_to_text([]) == ""


def test_chunk_text_splits_long_text_under_limit():
    para = "句子。" * 200  # 600 chars
    chunks = notion.chunk_text(para, max_chars=300)
    assert len(chunks) >= 2
    assert all(len(c) <= 300 for c in chunks)
    # No content lost.
    assert "".join(chunks).replace("\n", "") == para


def test_chunk_text_keeps_short_text_as_one_chunk():
    assert notion.chunk_text("短文本", max_chars=300) == ["短文本"]


def test_chunk_text_ignores_blank():
    assert notion.chunk_text("   \n  ", max_chars=300) == []
