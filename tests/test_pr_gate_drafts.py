"""drafts: skip passes a run unjudged only when its event and live state say draft."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _prgate import TEMPLATE, _api, _gate_module, _issue, _valid_body  # noqa: E402

# The issue is unassigned, so a judged run refuses and comments.
JUDGED, SKIPPED = 2, 0


def _gate(*, live_draft, **options):
    api = _api(draft=live_draft, issues={'101': _issue('bob')})
    api.pull['body'] = _valid_body()
    gate = _gate_module()
    assert gate is not None
    return gate.run(api, 'owner/repo', '99', 'alice', TEMPLATE, **options), api


def test_skip_passes_only_a_run_whose_event_and_live_state_both_say_draft(tmp):
    del tmp
    for event_draft, live_draft, expected in (
            (False, False, JUDGED), (False, True, JUDGED),
            (True, False, JUDGED), (True, True, SKIPPED)):
        code, api = _gate(live_draft=live_draft, skip_drafts=True, event_draft=event_draft)
        assert code == expected, (event_draft, live_draft)
        if expected == SKIPPED:
            assert api.calls == [('GET', 'repos/owner/repo/pulls/99', None)]
        else:
            assert [call[0] for call in api.writes] == ['POST'], (event_draft, live_draft)


def test_the_default_judges_a_draft_whatever_its_event_says(tmp):
    del tmp
    for event_draft in (False, True):
        code, api = _gate(live_draft=True, event_draft=event_draft)
        assert code == JUDGED and len(api.writes) == 1, event_draft
