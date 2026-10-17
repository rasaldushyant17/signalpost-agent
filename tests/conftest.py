"""Shared pytest fixtures.

registry_brreg.py deliberately waits several real seconds between retries
when the live Norwegian registry returns a 404 (see its module docstring
for why -- a live 1,000-company run showed this needs to be patient, not
quick, to have any chance of working). That patience is correct in
production but would make the test suite take minutes, since many tests
use a fake "unstubbed" 404 to represent an unavailable source and don't
care about real wall-clock timing at all.

This fixture neutralizes that one real sleep call for every test
automatically, so the suite stays fast while production behaviour (driven
through scripts/run_batch.py, not through pytest) is completely unaffected.

IMPORTANT: this must patch registry_brreg's own NAME binding for the
`time` module, not an attribute on the real `time` module object itself.
`import time` everywhere in the process shares the exact same module
object, so `monkeypatch.setattr(reg.time, "sleep", ...)` would silently
replace time.sleep GLOBALLY for the whole test run -- which briefly broke
two unrelated tests elsewhere that also use time.sleep for their own,
real timing logic. Patching `reg`'s local name instead keeps the effect
scoped to registry_brreg.py only.
"""
import types

import pytest

from signalpost_agent import registry_brreg as reg


@pytest.fixture(autouse=True)
def _no_real_backoff_sleep_in_tests(monkeypatch):
    fake_time = types.SimpleNamespace(sleep=lambda _seconds: None)
    monkeypatch.setattr(reg, "time", fake_time)
