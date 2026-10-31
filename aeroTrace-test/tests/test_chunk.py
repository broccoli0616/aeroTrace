from aerotrace.chunk import structure_chunks


def test_structure_chunks_preserves_sections():
    text = "Header\n\n(a) Comments Due Date\nComments close tomorrow.\n\n(c) Applicability\nThis AD applies to Model X."
    chunks = structure_chunks(text)
    sections = {chunk["section"] for chunk in chunks}
    assert "(a) Comments Due Date" in sections
    assert "(c) Applicability" in sections


def test_long_sections_split_without_crossing_header():
    body = "Sentence with useful evidence. " * 200
    chunks = structure_chunks(f"(c) Applicability\n{body}\n(d) Subject\nA short subject statement with enough content.")
    assert len([c for c in chunks if "Applicability" in c["section"]]) > 1
    assert all("Subject" not in c["text"] for c in chunks if "Applicability" in c["section"])

