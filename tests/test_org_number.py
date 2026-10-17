from signalpost_agent import org_number as onr


def test_valid_real_numbers():
    assert onr.is_valid("923609016")
    assert onr.is_valid("915637353")


def test_invalid_checksum():
    assert not onr.is_valid("123456789")


def test_invalid_length():
    assert not onr.is_valid("12345")
    assert not onr.is_valid("1234567890")


def test_normalize_strips_non_digits():
    assert onr.normalize("923 609 016") == "923609016"
    assert onr.normalize("NO923609016MVA") == "923609016"


def test_parse_or_none():
    assert onr.parse_or_none("923 609 016") == "923609016"
    assert onr.parse_or_none("123456789") is None
    assert onr.parse_or_none("") is None


def test_find_all_contextual_labelled():
    text = "Kontakt oss. Org.nr. 923 609 016. Telefon: 22334455."
    hits = onr.find_all_contextual(text)
    assert len(hits) == 1
    assert hits[0]["number"] == "923609016"


def test_find_all_contextual_vat_style():
    text = "Foretaksregisteret NO 923609016 MVA"
    hits = onr.find_all_contextual(text)
    assert len(hits) == 1
    assert hits[0]["number"] == "923609016"


def test_find_all_contextual_rejects_unlabelled_digits():
    # The exact bug reproduced in the reference kit: a bare digit substring
    # (e.g. from a phone number) must NOT be treated as a contextual key.
    text = "Ring oss på 92 36 09 016 eller besøk kontoret."
    hits = onr.find_all_contextual(text)
    assert hits == []


def test_find_all_contextual_rejects_invalid_checksum_even_if_labelled():
    text = "Org.nr. 123456789"
    hits = onr.find_all_contextual(text)
    assert hits == []


def test_find_all_contextual_multiple_entities_in_one_block():
    # The group-listing trap: several labelled, valid numbers in one block.
    text = (
        "Våre selskaper: Datterselskap Ett AS (org.nr. 923609016), "
        "Datterselskap To AS (org.nr. 915637353)."
    )
    hits = onr.find_all_contextual(text)
    numbers = {h["number"] for h in hits}
    assert numbers == {"923609016", "915637353"}
