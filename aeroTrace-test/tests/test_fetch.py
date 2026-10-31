from aerotrace.fetch import document_number


def test_document_number_from_full_and_short_urls():
    assert document_number("https://www.federalregister.gov/documents/2026/10/19/2026-21387/example") == "2026-21387"
    assert document_number('Withdrawal: https://www.federalregister.gov/d/2021-01771') == "2021-01771"


def test_document_number_ignores_pasted_text():
    assert document_number("The Withdrawal. Accordingly, the FAA withdraws this NPRM.") is None

