from signalpost_agent.identity import (
    assess_website_identity, check_redirect_domain, assess_social_link,
)

LONG_FILLER = "Vi leverer tjenester til kunder over hele landet og har gjort det i mange år. " * 3


def test_W1_subsidiary_tokens_subset_of_group_title_is_not_published():
    # Reproduces IDENTITY_SAFETY.md W1: old gate returned exact/0.95.
    decision = assess_website_identity(
        target_org_number="900000001",
        target_legal_name="BERGEN BYGG AS",
        candidate_host="www.bergenbyggkonsern.no",
        homepage_title="Bergen Bygg Konsern | Bygg, eiendom og entreprenør",
        homepage_text=LONG_FILLER,
    )
    assert decision.decision != "publish"


def test_W2_single_token_name_on_unrelated_site_is_not_published():
    # Reproduces IDENTITY_SAFETY.md W2: old gate returned exact/0.95.
    decision = assess_website_identity(
        target_org_number="900000002",
        target_legal_name="FJORD AS",
        candidate_host="www.fjordcruise-tours.no",
        homepage_title="Fjord Cruise Tours - opplev fjordene",
        homepage_text=LONG_FILLER,
    )
    assert decision.decision != "publish"


def test_W3_group_listing_with_org_numbers_is_ambiguous_not_exact():
    # Reproduces IDENTITY_SAFETY.md W3 / test #33: old gate returned exact/1.0
    # for EVERY subsidiary listed on a shared homepage.
    # (900000014 / 900000030 / 900000022 are mod-11-valid synthetic numbers.)
    text = (
        "Våre selskaper: Datterselskap Ett AS (org.nr. 900 000 014), "
        "Datterselskap Tre AS (org.nr 900 000 030), "
        "Datterselskap To AS (org.nr 900 000 022). " + LONG_FILLER
    )
    decision = assess_website_identity(
        target_org_number="900000030",
        target_legal_name="DATTERSELSKAP TRE AS",
        candidate_host="www.konsern.no",
        homepage_title="Konsern",
        homepage_text=text,
    )
    assert decision.decision == "ambiguous"
    assert decision.decision != "publish"


def test_case3_the_same_shared_page_rejects_a_company_not_even_listed():
    text = (
        "Våre selskaper: Datterselskap Ett AS (org.nr. 900 000 014), "
        "Datterselskap To AS (org.nr 900 000 022)."
    )
    decision = assess_website_identity(
        target_org_number="999999999",  # not one of the listed numbers
        target_legal_name="HELT ANNET SELSKAP AS",
        candidate_host="www.konsern.no",
        homepage_title="Konsern",
        homepage_text=text,
    )
    assert decision.decision == "reject"


def test_org_number_in_contextual_label_publishes_K1():
    decision = assess_website_identity(
        target_org_number="923609016",
        target_legal_name="KAFFEBRENNERIET DRIFT AS",
        candidate_host="www.kaffebrenneriet.no",
        homepage_title="Kaffebrenneriet",
        homepage_text=f"Kontakt oss. Org.nr. 923 609 016. {LONG_FILLER}",
    )
    assert decision.decision == "publish"
    assert decision.tier == "K1"


def test_phone_number_digits_never_promote_identity():
    # A bare digit substring (from a phone number) must never be treated
    # as a contextual organisation-number key.
    decision = assess_website_identity(
        target_org_number="922334455",
        target_legal_name="NOEN ANDRE AS",
        candidate_host="www.noenandre-x.no",
        homepage_title="Kontakt oss",
        homepage_text="Ring oss på 92 23 34 45 5 eller besøk kontoret. Åpent 08-16.",
    )
    assert decision.decision != "publish"


def test_directory_host_always_rejected():
    decision = assess_website_identity(
        target_org_number="923609016",
        target_legal_name="EXAMPLE AS",
        candidate_host="www.proff.no",
        homepage_title="Example AS - Proff.no",
        homepage_text=f"Org.nr. 923 609 016. {LONG_FILLER}",
    )
    assert decision.decision == "reject"


def test_parked_page_rejected_even_with_matching_name():
    decision = assess_website_identity(
        target_org_number="923609016",
        target_legal_name="EXAMPLE AS",
        candidate_host="example.no",
        homepage_title="example.no",
        homepage_text="This domain is parked. Buy this domain today!",
    )
    assert decision.decision == "reject"


def test_full_legal_name_alone_is_retained_not_published():
    # Tier N (name-only) is a supporting signal, never sufficient alone in v1.
    decision = assess_website_identity(
        target_org_number="900000005",
        target_legal_name="NORDIC BYGG AS",
        candidate_host="www.nordicbygg.no",
        homepage_title="Nordic Bygg AS",
        homepage_text=LONG_FILLER,
    )
    assert decision.decision == "retain"
    assert decision.decision != "publish"


def test_similar_name_with_extra_word_is_not_a_match():
    # "NORDIC BYGG AS" vs "NORDIC BYGG HOLDING AS" -- legal form/full name
    # comparison must not fuzzy-match a superset name.
    decision = assess_website_identity(
        target_org_number="900000005",
        target_legal_name="NORDIC BYGG AS",
        candidate_host="www.nordicbyggholding.no",
        homepage_title="Nordic Bygg Holding AS",
        homepage_text=LONG_FILLER,
    )
    assert decision.decision != "publish"


def test_known_subunit_number_is_retained_not_published_for_parent():
    # 900000103 / 900000111 are mod-11-valid synthetic numbers.
    decision = assess_website_identity(
        target_org_number="900000103",  # the PARENT
        target_legal_name="PARENT GROUP AS",
        candidate_host="www.parentgroup.no",
        homepage_title="Parent Group",
        homepage_text=f"Vår avdeling: org.nr. 900000111. {LONG_FILLER}",
        known_subunit_numbers={"900000111"},
    )
    assert decision.decision != "publish"





def test_redirect_to_different_registered_domain_is_flagged():
    decision = check_redirect_domain("example.no", "totallydifferent.com")
    assert decision is not None
    assert decision.decision == "reject"


def test_redirect_to_same_registered_domain_is_not_flagged():
    decision = check_redirect_domain("example.no", "example.no")
    assert decision is None


def test_social_profile_published_only_when_linked_from_verified_site():
    published = assess_social_link(link_url="https://www.linkedin.com/company/example",
                                    was_linked_from_verified_site=True)
    assert published.decision == "publish"
    assert published.tier == "L"

    not_published = assess_social_link(link_url="https://www.linkedin.com/company/example",
                                        was_linked_from_verified_site=False)
    assert not_published.decision != "publish"
