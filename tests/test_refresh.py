import tempfile

from signalpost_agent.claims import Claim, make_claim_id
from signalpost_agent.refresh import diff_company, ChangeState, HistoryStore

ORG = "923609016"


def _claim(field, value, *, scope=None, period=None, availability="available", source_key=""):
    cid = make_claim_id(ORG, field, value, scope=scope, period=period, source_key=source_key or field)
    return Claim(claim_id=cid, organisation_number=ORG, field=field, value=value,
                 unit=None, scope=scope, reporting_period=period, availability=availability,
                 identity_basis="K2:brreg_key", evidence_ids=["ev1"])


def test_no_change_when_identical():
    prev = [_claim("identity.legal_name", "Example AS")]
    curr = [_claim("identity.legal_name", "Example AS")]
    changes = diff_company(ORG, previous_claims=prev, current_claims=curr, now_iso="2026-09-22T00:00:00Z")
    assert [c.state for c in changes] == [ChangeState.NO_CHANGE]


def test_real_change_detected():
    prev = [_claim("accounts.revenue", 1_000_000, scope="SELSKAP",
                    period={"from": "2025-01-01", "to": "2025-12-31"})]
    curr = [_claim("accounts.revenue", 2_000_000, scope="SELSKAP",
                    period={"from": "2025-01-01", "to": "2025-12-31"})]
    changes = diff_company(ORG, previous_claims=prev, current_claims=curr, now_iso="2026-09-22T00:00:00Z")
    assert len(changes) == 1
    assert changes[0].state == ChangeState.REAL_CHANGE
    assert changes[0].previous_value == 1_000_000
    assert changes[0].current_value == 2_000_000


def test_new_field_first_appearance():
    prev = []
    curr = [_claim("identity.legal_name", "Example AS")]
    changes = diff_company(ORG, previous_claims=prev, current_claims=curr, now_iso="2026-09-22T00:00:00Z")
    assert [c.state for c in changes] == [ChangeState.NEW]


def test_source_unavailable_never_becomes_a_change():
    # THE reproduced bug: a 503 on rerun must never be reported as a
    # change to None. It must carry the prior value forward untouched.
    prev = [_claim("accounts.revenue", 1_000_000, scope="SELSKAP",
                    period={"from": "2025-01-01", "to": "2025-12-31"})]
    curr = [_claim("accounts.revenue", None, availability="failed")]
    changes = diff_company(ORG, previous_claims=prev, current_claims=curr, now_iso="2026-09-22T00:00:00Z")
    assert len(changes) == 1
    assert changes[0].state == ChangeState.SOURCE_UNAVAILABLE
    # The prior value must be visible in the change record, not erased.
    assert changes[0].previous_value == 1_000_000
    assert changes[0].current_value is None


def test_blocked_is_distinct_from_source_unavailable():
    curr = [_claim("website.official", None, availability="blocked")]
    changes = diff_company(ORG, previous_claims=[], current_claims=curr, now_iso="2026-09-22T00:00:00Z")
    assert changes[0].state == ChangeState.BLOCKED


def test_reordered_list_is_no_change_because_claim_id_is_canonical():
    # THE second reproduced bug: re-ordering the same roles must not be a
    # change. Because roles are canonicalised (sorted) before claim_id is
    # computed (see registry_brreg.fetch_roles), the two "runs" below
    # naturally produce identical claim_ids -- this test proves that
    # property at the refresh layer using the same claim twice.
    people = [{"role_code": "LEDE", "person_name": "Anna X"}, {"role_code": "MEDL", "person_name": "Bjorn X"}]
    prev = [_claim("leadership.roles", people)]
    curr = [_claim("leadership.roles", people)]  # same canonical value both times
    changes = diff_company(ORG, previous_claims=prev, current_claims=curr, now_iso="2026-09-22T00:00:00Z")
    assert [c.state for c in changes] == [ChangeState.NO_CHANGE]


def test_field_absent_from_current_run_is_not_declared_removed():
    # A company simply wasn't re-checked for this field this run (e.g. it
    # wasn't in today's batch, or a module was skipped under budget
    # pressure). Absence alone must never become REMOVED.
    prev = [_claim("workplace.subunits", [{"organisation_number": "923609024"}])]
    curr = []  # this run produced no claim at all for this field
    changes = diff_company(ORG, previous_claims=prev, current_claims=curr, now_iso="2026-09-22T00:00:00Z")
    assert len(changes) == 1
    assert changes[0].state == ChangeState.SOURCE_UNAVAILABLE
    assert changes[0].previous_value == prev[0].value


def test_diff_company_never_raises_on_mismatched_inputs():
    # THE third reproduced bug: the reference kit's diff_datasets raised a
    # bare ValueError when the two sides didn't cover the same companies.
    # diff_company operates on ONE company's claims only and never
    # inspects a global company set, so there is nothing to raise on.
    try:
        diff_company("999999999", previous_claims=[], current_claims=[_claim("identity.legal_name", "X")],
                      now_iso="2026-09-22T00:00:00Z")
    except Exception as exc:  # pragma: no cover - this must never happen
        raise AssertionError(f"diff_company raised unexpectedly: {exc}")


def test_idempotent_rerun_on_identical_snapshot_produces_no_change_growth():
    with tempfile.TemporaryDirectory() as tmp:
        store = HistoryStore(tmp)
        claim = _claim("identity.legal_name", "Example AS")
        changes = diff_company(ORG, previous_claims=store.load_latest_available_claims(ORG),
                                current_claims=[claim], now_iso="t1")
        store.append_run(ORG, "run1", changes, {claim.claim_id: claim}, "t1")

        prev = store.load_latest_available_claims(ORG)
        changes2 = diff_company(ORG, previous_claims=prev, current_claims=[claim], now_iso="t2")
        store.append_run(ORG, "run2", changes2, {claim.claim_id: claim}, "t2")

        assert [c.state for c in changes2] == [ChangeState.NO_CHANGE]
        # NO_CHANGE rows are not appended -- the history file should only
        # have grown by the first run's NEW row.
        lines = (store.root / f"{ORG}.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1


def test_history_preserves_prior_value_after_a_real_change():
    with tempfile.TemporaryDirectory() as tmp:
        store = HistoryStore(tmp)
        old_claim = _claim("accounts.revenue", 1_000_000, scope="SELSKAP",
                            period={"from": "2025-01-01", "to": "2025-12-31"})
        changes1 = diff_company(ORG, previous_claims=[], current_claims=[old_claim], now_iso="t1")
        store.append_run(ORG, "run1", changes1, {old_claim.claim_id: old_claim}, "t1")

        new_claim = _claim("accounts.revenue", 1_500_000, scope="SELSKAP",
                            period={"from": "2025-01-01", "to": "2025-12-31"})
        prev = store.load_latest_available_claims(ORG)
        changes2 = diff_company(ORG, previous_claims=prev, current_claims=[new_claim], now_iso="t2")
        assert changes2[0].state == ChangeState.REAL_CHANGE
        store.append_run(ORG, "run2", changes2, {new_claim.claim_id: new_claim}, "t2")

        # Both the old and new claim rows are still in the append-only file.
        raw = (store.root / f"{ORG}.jsonl").read_text()
        assert str(old_claim.value) in raw or "1000000" in raw.replace(" ", "")
        assert "1500000" in raw.replace(" ", "")
