# SPDX-License-Identifier: MIT
"""release/acceptance.json against tools/rungic_acceptance.py, offline: every scenario names a check
that exists and takes its parameters, so a renamed or removed check fails here, not on the phone
(cast.agent_screen named agent_screen_output for days after it was gone)."""
import inspect
import json
from pathlib import Path
import types

import pytest

import rungic_acceptance

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = json.loads((ROOT / 'release/acceptance.json').read_text())['scenarios']


# covers: delivery.acceptance
def test_every_scenario_names_a_check_that_takes_its_parameters():
    for scenario in SCENARIOS:
        fn = rungic_acceptance.CHECKS.get(scenario['check'])
        assert fn, f"{scenario['id']}: no check {scenario['check']!r} in tools/rungic_acceptance.py"
        inspect.signature(fn).bind(None, **scenario.get('params', {}))


# covers: delivery.acceptance
def test_scenarios_are_unique_and_run_at_a_known_level():
    ids = [s['id'] for s in SCENARIOS]
    assert len(ids) == len(set(ids)), 'a scenario id is used twice'
    assert {s['level'] for s in SCENARIOS} <= {'smoke', 'full'}


KGSL_IDS = {'contract.gpu-device', 'recording.quicksetting', 'perf.compositor', 'contract.wifi-display'}


# covers: delivery.acceptance/E6
def test_only_kgsl_dependent_scenarios_declare_a_device_and_a_reason():
    # Pixel 8 Pro (husky/Mali) must be accepted without treating Adreno-only
    # features as either broken or verified. Keep the checks for Qualcomm phones.
    gated = {s['id']: s['requires_character_device'] for s in SCENARIOS if 'requires_character_device' in s}
    assert set(gated) == KGSL_IDS
    for requirement in gated.values():
        assert requirement['path'] == '/dev/kgsl-3d0'
        assert requirement['reason'].strip()


@pytest.fixture
def acceptance_phone(tmp_path, monkeypatch):
    state = types.SimpleNamespace(probe='present', commands=[], checks=[], screenshots=[])
    monkeypatch.setattr(rungic_acceptance, 'RESULTS', tmp_path)
    monkeypatch.setattr(rungic_acceptance, 'bring_to_front', lambda: {})
    monkeypatch.setattr(rungic_acceptance, 'previous_report', lambda *args: (None, None))

    def run(script, level='root', timeout=60, check=True):
        state.commands.append((script, level))
        if isinstance(state.probe, Exception):
            raise state.probe
        return types.SimpleNamespace(stdout=state.probe + '\n', stderr='', returncode=0)

    def screenshot():
        state.screenshots.append(True)
        raise RuntimeError('no screen')

    monkeypatch.setattr(rungic_acceptance, 'run', run)
    monkeypatch.setattr(rungic_acceptance.rungic_agent, 'screenshot', screenshot)
    return state


def kgsl_scenarios():
    return [s for s in SCENARIOS if s['id'] in KGSL_IDS]


def stand_in_checks(monkeypatch, phone, passed=True):
    def check(ctx, **params):
        phone.checks.append(params)
        return rungic_acceptance.result(passed, {'sample': 7})
    for scenario in kgsl_scenarios():
        monkeypatch.setitem(rungic_acceptance.CHECKS, scenario['check'], check)
    monkeypatch.setitem(rungic_acceptance.CHECKS, 'unrelated', check)


# covers: delivery.acceptance/E6
def test_husky_reports_not_applicable_without_running_qualcomm_checks(acceptance_phone, monkeypatch, capsys):
    # husky DOES have /dev/dma_heap/system. Gate on Android's KGSL node alone,
    # before recording/performance checks can touch UI or create recordings.
    phone = acceptance_phone
    phone.probe = 'absent'
    stand_in_checks(monkeypatch, phone)
    selected = kgsl_scenarios() + [dict(id='unrelated', title='generic', level='full', check='unrelated')]
    report = rungic_acceptance.run_scenarios(selected)
    assert phone.checks == [{}], 'only the unrelated check should run on husky'
    assert len(phone.commands) == 1, 'one consistent read-only capability snapshot'
    script, level = phone.commands[0]
    assert level == 'root' and '-c /dev/kgsl-3d0' in script
    assert '/dev/dma_heap/system' not in script
    for row in report['scenarios'][:-1]:
        assert row['passed'] is None and row['status'] == 'not applicable'
        assert row['details']['reason'] == next(s['requires_character_device']['reason']
                                               for s in selected if s['id'] == row['id'])
        assert row['metrics'] == {}, 'unrun checks cannot contribute performance metrics'
    assert report['passed'] is True and report['complete'] is True
    assert set(report['not_applicable_ids']) == KGSL_IDS
    assert report['skipped_ids'] == [] and report['failed_ids'] == []
    assert phone.screenshots == []
    saved = json.loads(Path(report['path']).read_text())
    assert saved['not_applicable_ids'] == report['not_applicable_ids']
    output = capsys.readouterr().out
    for scenario in kgsl_scenarios():
        assert f"N/A {scenario['id']}" in output
        assert f"PASS {scenario['id']}" not in output
        assert scenario['requires_character_device']['reason'] in output


# covers: delivery.acceptance/E6
@pytest.mark.parametrize('passed', [True, False])
def test_qualcomm_runs_all_existing_checks_and_preserves_failures(acceptance_phone, monkeypatch, passed):
    phone = acceptance_phone
    stand_in_checks(monkeypatch, phone, passed)
    report = rungic_acceptance.run_scenarios(kgsl_scenarios())
    assert len(phone.checks) == 4
    assert report['passed'] is passed and report['complete'] is True
    assert report['not_applicable_ids'] == [] and report['skipped_ids'] == []
    assert set(report['failed_ids']) == (set() if passed else KGSL_IDS)
    assert all(row['passed'] is passed and row['metrics'] == {'sample': 7} for row in report['scenarios'])


# covers: delivery.acceptance/E6
@pytest.mark.parametrize('probe', ['', 'unexpected', 'invalid', RuntimeError('device disconnected')])
def test_unknown_capability_fails_instead_of_hiding_qualcomm_failures(acceptance_phone, monkeypatch, probe):
    phone = acceptance_phone
    phone.probe = probe
    stand_in_checks(monkeypatch, phone)
    report = rungic_acceptance.run_scenarios(kgsl_scenarios())
    assert phone.checks == []
    assert report['passed'] is False and set(report['failed_ids']) == KGSL_IDS
    assert report['not_applicable_ids'] == []


# covers: delivery.acceptance/E6
def test_no_applicable_checks_cannot_be_reported_as_passed(acceptance_phone, monkeypatch):
    acceptance_phone.probe = 'absent'
    stand_in_checks(monkeypatch, acceptance_phone)
    report = rungic_acceptance.run_scenarios(kgsl_scenarios())
    assert report['passed'] is False and report['complete'] is True
    assert report['failed_ids'] == [] and set(report['not_applicable_ids']) == KGSL_IDS


# covers: delivery.acceptance/E6
def test_an_unimplemented_check_and_explicit_skip_stay_skipped(acceptance_phone, monkeypatch):
    scenarios = [dict(id='missing', title='missing', level='full', check='no_such_check'),
                 dict(id='excluded', title='excluded', level='full', check='no_such_check')]
    report = rungic_acceptance.run_scenarios(scenarios, skips={'excluded': 'user scope'})
    assert report['passed'] is False and report['complete'] is False
    assert report['skipped_ids'] == ['missing', 'excluded']
    assert report['not_applicable_ids'] == [] and acceptance_phone.commands == []


# covers: delivery.acceptance/E6
def test_qualcomm_missing_container_bind_is_a_contract_failure(acceptance_phone, monkeypatch):
    # Probe Android root first: a missing bind inside a Qualcomm container is a
    # deployment defect, not evidence that this is a non-Qualcomm phone.
    def run(script, level='root', timeout=60, check=True):
        acceptance_phone.commands.append((script, level))
        return types.SimpleNamespace(stdout='present\n' if level == 'root' else '', stderr='',
                                     returncode=0 if level == 'root' else 1)
    monkeypatch.setattr(rungic_acceptance, 'run', run)
    selected = [s for s in kgsl_scenarios() if s['id'] == 'contract.gpu-device']
    report = rungic_acceptance.run_scenarios(selected)
    assert report['failed_ids'] == ['contract.gpu-device'] and report['not_applicable_ids'] == []
    assert 'kgsl' in report['scenarios'][0]['details']['problems']
    assert acceptance_phone.commands[0][1] == 'root'
    assert any(level == 'user' for _, level in acceptance_phone.commands)
