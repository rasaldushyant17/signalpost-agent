from signalpost_agent.discovery import candidate_domains


def test_single_token_legal_name_strips_legal_form_and_guesses_no_then_com():
    domains = candidate_domains("ELOPAK ASA")
    assert domains[0] == "elopak.no"
    assert "elopak.com" in domains
    # Single token: hyphenated form is identical to the joined form, so it
    # must not appear twice.
    assert domains.count("elopak.no") == 1


def test_multi_token_legal_name_produces_joined_and_hyphenated_variants():
    domains = candidate_domains("MTM SKOGSERVICE AS")
    assert domains[0] == "mtmskogservice.no"
    assert "mtm-skogservice.no" in domains
    assert "mtmskogservice.com" in domains
    assert len(domains) <= 4


def test_multi_word_name_also_tries_first_token_as_a_lower_priority_guess():
    # General pattern (not company-specific): many multi-word Norwegian
    # organisations are commonly reachable at just their first, most
    # distinctive word (e.g. a "... SYKEHUS HF" hospital trust at the
    # short form of its name) -- tried last, after the full-name guesses.
    domains = candidate_domains("SUNNAAS SYKEHUS HF")
    assert domains[-1] == "sunnaas.no"
    assert domains[0] == "sunnaassykehus.no"


def test_norwegian_characters_are_ascii_folded():
    domains = candidate_domains("GRØNN ELEKTRO AS")
    assert domains[0] == "gronnelektro.no"
    assert all(d.isascii() for d in domains)


def test_filler_tokens_and_legal_form_are_dropped():
    domains = candidate_domains("NORSK OG INTERNASJONAL HANDEL AS")
    assert domains[0] == "norskinternasjonalhandel.no"
    assert "og" not in domains[0]


def test_empty_or_unusable_name_returns_no_candidates():
    assert candidate_domains("") == []
    assert candidate_domains("AS") == []  # legal form only, nothing left


def test_result_is_capped_at_max_candidates():
    domains = candidate_domains("ALPHA BETA GAMMA DELTA EPSILON AS", max_candidates=2)
    assert len(domains) <= 2


def test_candidates_are_deterministic_across_calls():
    # Same input must always produce the same output, in the same order --
    # discovery must never introduce run-to-run non-determinism.
    first = candidate_domains("EQUINOR ASA")
    second = candidate_domains("EQUINOR ASA")
    assert first == second
