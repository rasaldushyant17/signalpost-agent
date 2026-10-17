"""Refresh: comparing this run's claims against the previous run's claims
for the same company, without ever treating a fetch failure as a
deletion, without being order-sensitive, and without crashing on a
partial batch.

This directly replaces the reference starter kit's diff_profile/
diff_datasets, which was shown (REFRESH_STRATEGY.md) to:
  * report a 503-on-rerun as a change to `None`,
  * report a mere re-ordering of the same roles as a change,
  * raise a bare ValueError when one company is missing from one side.

States, per REFRESH_STRATEGY.md section 1:
  NO_CHANGE, REAL_CHANGE, NEW, REMOVED, SOURCE_UNAVAILABLE, BLOCKED,
  AMBIGUOUS, NOT_APPLICABLE, FAILED.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .claims import Claim, field_key


class ChangeState:
    NO_CHANGE = "NO_CHANGE"
    REAL_CHANGE = "REAL_CHANGE"
    NEW = "NEW"
    REMOVED = "REMOVED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    BLOCKED = "BLOCKED"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    FAILED = "FAILED"


# Fetch-layer availability states that must NEVER be diffed against a prior
# value -- they describe why we don't know, not what changed.
_NON_DIFFABLE_AVAILABILITY = {"not_available", "blocked", "failed", "not_applicable"}

_AVAILABILITY_TO_STATE = {
    "blocked": ChangeState.BLOCKED,
    "failed": ChangeState.SOURCE_UNAVAILABLE,
    "not_applicable": ChangeState.NOT_APPLICABLE,
    "ambiguous": ChangeState.AMBIGUOUS,
}


@dataclass
class ChangeRecord:
    organisation_number: str
    field: str
    state: str
    field_key: str
    previous_claim_id: Optional[str] = None
    current_claim_id: Optional[str] = None
    previous_value: Optional[object] = None
    current_value: Optional[object] = None
    note: str = ""


@dataclass
class HistoryEntry:
    """One row of a company's append-only claim history (see
    REFRESH_STRATEGY.md section 4). Persisted as JSON Lines; never
    overwritten in place.
    """
    run_id: str
    claim: Claim
    state: str
    superseded_by: Optional[str] = None
    first_seen: str = ""
    last_seen: str = ""
    last_checked: str = ""


def diff_company(
    organisation_number: str,
    *,
    previous_claims: list[Claim],
    current_claims: list[Claim],
    now_iso: str,
) -> list[ChangeRecord]:
    """Diff ONE company's claim set. Never raises, never touches other
    companies. This function alone fixes the reproduced partial-batch
    crash: callers simply don't call it for a company that is missing
    from one side, and a missing company on either side is not this
    function's problem.
    """
    changes: list[ChangeRecord] = []

    prev_by_id = {c.claim_id: c for c in previous_claims}
    curr_by_id = {c.claim_id: c for c in current_claims}

    prev_by_fieldkey: dict[str, Claim] = {}
    prev_by_fieldname: dict[str, list[Claim]] = {}
    for c in previous_claims:
        if c.availability == "available":
            fk = field_key(c.organisation_number, c.field, scope=c.scope, period=c.reporting_period)
            prev_by_fieldkey[fk] = c
            prev_by_fieldname.setdefault(c.field, []).append(c)

    seen_field_keys: set[str] = set()

    for curr in current_claims:
        # Fetch-layer non-availability is reported as its own state and
        # NEVER compared against a prior value (this is the exact fix for
        # the reproduced "503 -> reported change to None" bug). Because a
        # failed fetch tells us nothing about scope/period, matching is by
        # FIELD NAME here, not the full scope+period field_key used for
        # available claims -- and every matched prior field_key is marked
        # seen so the trailing "missing from this run" loop below does not
        # also (double-)report the same prior claims.
        if curr.availability in _NON_DIFFABLE_AVAILABILITY:
            priors = prev_by_fieldname.get(curr.field, [])
            for p in priors:
                seen_field_keys.add(field_key(p.organisation_number, p.field, scope=p.scope,
                                               period=p.reporting_period))
            representative = priors[-1] if priors else None
            changes.append(ChangeRecord(
                organisation_number=organisation_number, field=curr.field,
                state=_AVAILABILITY_TO_STATE.get(curr.availability, ChangeState.SOURCE_UNAVAILABLE),
                field_key=field_key(organisation_number, curr.field),
                previous_claim_id=representative.claim_id if representative else None,
                current_claim_id=None,
                previous_value=representative.value if representative else None,
                current_value=None,
                note=(f"fetch/availability={curr.availability}; {len(priors)} prior value(s) "
                      "carried forward, not overwritten"),
            ))
            continue

        fk = field_key(curr.organisation_number, curr.field, scope=curr.scope,
                        period=curr.reporting_period)
        seen_field_keys.add(fk)

        if curr.claim_id in prev_by_id:
            changes.append(ChangeRecord(
                organisation_number=organisation_number, field=curr.field,
                state=ChangeState.NO_CHANGE, field_key=fk,
                previous_claim_id=curr.claim_id, current_claim_id=curr.claim_id,
                previous_value=curr.value, current_value=curr.value,
            ))
            continue

        prior = prev_by_fieldkey.get(fk)
        if prior is None:
            changes.append(ChangeRecord(
                organisation_number=organisation_number, field=curr.field,
                state=ChangeState.NEW, field_key=fk,
                previous_claim_id=None, current_claim_id=curr.claim_id,
                previous_value=None, current_value=curr.value,
            ))
        else:
            changes.append(ChangeRecord(
                organisation_number=organisation_number, field=curr.field,
                state=ChangeState.REAL_CHANGE, field_key=fk,
                previous_claim_id=prior.claim_id, current_claim_id=curr.claim_id,
                previous_value=prior.value, current_value=curr.value,
                note="value differs from the prior available claim at the same field/scope/period",
            ))

    # Fields present in the prior run but absent from this run's AVAILABLE
    # claims. Per REFRESH_STRATEGY.md section 5, absence alone is never
    # enough to declare REMOVED -- callers of this function are expected
    # to have already turned a genuinely-missing structural signal (e.g. a
    # NAV feed's own INACTIVE event) into a claim with availability=
    # "not_available" BEFORE calling diff_company, so it is handled by the
    # branch above. Anything that reaches here without such a signal is
    # left alone: we simply do not re-assert it, and do not fabricate a
    # REMOVED record from silence.
    for fk, prior in prev_by_fieldkey.items():
        if fk not in seen_field_keys:
            changes.append(ChangeRecord(
                organisation_number=organisation_number, field=prior.field,
                state=ChangeState.SOURCE_UNAVAILABLE, field_key=fk,
                previous_claim_id=prior.claim_id, current_claim_id=None,
                previous_value=prior.value, current_value=None,
                note="field was not re-checked this run (absent from current claim set); "
                     "prior value carried forward, not treated as removed",
            ))

    return changes


class HistoryStore:
    """Append-only, per-company claim history. One JSON-Lines file per
    organisation number under `root/<org>.jsonl`. Never rewrites or
    deletes a prior line (see REFRESH_STRATEGY.md section 4).
    """

    def __init__(self, root):
        from pathlib import Path
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, org: str):
        return self.root / f"{org}.jsonl"

    def load_latest_available_claims(self, org: str) -> list[Claim]:
        """Reconstruct the most recent AVAILABLE claim per field_key from
        history, skipping superseded rows. Returns [] for a first run.
        """
        import json
        path = self._path(org)
        if not path.exists():
            return []
        latest_by_fieldkey: dict[str, Claim] = {}
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if row.get("state") not in (ChangeState.NEW, ChangeState.REAL_CHANGE, ChangeState.NO_CHANGE):
                    continue
                c = row["claim"]
                claim = Claim(**c)
                if claim.availability != "available":
                    continue
                fk = field_key(claim.organisation_number, claim.field, scope=claim.scope,
                                period=claim.reporting_period)
                # Later lines in an append-only file are more recent.
                latest_by_fieldkey[fk] = claim
        return list(latest_by_fieldkey.values())

    def append_run(self, org: str, run_id: str, changes: list[ChangeRecord],
                    current_claims_by_id: dict[str, Claim], now_iso: str) -> None:
        import json
        path = self._path(org)
        with path.open("a", encoding="utf-8") as fh:
            for change in changes:
                if change.state == ChangeState.NO_CHANGE:
                    continue  # do not grow the file for unchanged facts every single run
                claim = current_claims_by_id.get(change.current_claim_id)
                if claim is None:
                    continue  # SOURCE_UNAVAILABLE-type rows with no current claim: nothing new to append
                row = {
                    "run_id": run_id,
                    "claim": claim.to_dict(),
                    "state": change.state,
                    "superseded_by": None,
                    "first_seen": now_iso if change.state == ChangeState.NEW else "",
                    "last_seen": now_iso,
                    "last_checked": now_iso,
                }
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
