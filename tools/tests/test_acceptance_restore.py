# SPDX-License-Identifier: MIT
"""tools/rungic_acceptance.py on a stand-in phone: every scenario that changes something on the
user's phone puts it back, also when the check fails halfway (accessibility switched on for the
check, display scale, refresh policy, the input probe, the recording it made); and a run leaves its
report in .work/acceptance/<release>/<time>/report.json with the manual items that automatic
results do not replace."""
import json
import re
import sys
import time
import types
from pathlib import Path

import pytest

import rungic_acceptance as acc


class Result:
    def __init__(self, stdout='', returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, '', returncode


class Agent:
    """rungic_agent's AT-SPI tools: accessibility starts off; finding controls fails unless given."""

    def __init__(self, nodes=None):
        self.enabled = False
        self.switches = []
        self.nodes = nodes

    def a11y(self, what):
        return {'enabled': self.enabled} if what == 'state' else []

    def ui_enable(self, on):
        self.switches.append(on)
        self.enabled = on

    def ui_find(self, *args, **kwargs):
        if self.nodes is None:
            raise RuntimeError('the control is not there')
        return self.nodes

    def ui_tap(self, *args, **kwargs):
        return {}

    def ui_windows(self):
        return []


@pytest.fixture
def phone(monkeypatch):
    """Device commands: recorded; `answers` maps a part of a command to its reply."""
    state = types.SimpleNamespace(commands=[], answers={})

    def run(script, level='root', timeout=None, check=True):
        state.commands.append(script)
        for key, answer in state.answers.items():
            if key in script:
                return answer(script) if callable(answer) else answer
        return Result()
    monkeypatch.setattr(acc, 'run', run)
    # Waiting is asking a few times: the stand-in answers at once.
    monkeypatch.setattr(acc, 'wait_for', lambda condition, timeout=10, interval=0.5: condition() or condition())
    monkeypatch.setattr(acc, 'out', lambda script, level='root', timeout=None: run(script, level).stdout)
    monkeypatch.setattr(acc, 'user', lambda script, timeout=120: run(script, 'user'))
    monkeypatch.setattr(acc, '_home', lambda: None)
    monkeypatch.setattr(acc, 'time', types.SimpleNamespace(sleep=lambda s: None, time=time.time,
                                                          monotonic=time.monotonic))
    def missing(*args):
        raise RuntimeError('launcher not found')
    monkeypatch.setitem(sys.modules, 'ui_launch_check', types.SimpleNamespace(running=missing, open_drawer=missing))
    return state


def use_agent(monkeypatch, agent):
    monkeypatch.setattr(acc, 'rungic_agent', agent)
    return agent


# covers: delivery.acceptance/E3
@pytest.mark.parametrize('check', ['input_text', 'app_launch', 'rime_input', 'screen_recording'])
def test_accessibility_turned_on_for_a_check_is_turned_off_again(phone, monkeypatch, check):
    agent = use_agent(monkeypatch, Agent())
    try:
        acc.CHECKS[check]({})
    except RuntimeError:
        pass                                    # a check that fails halfway restores too
    assert agent.switches == [True, False]
    assert agent.enabled is False


# covers: delivery.acceptance/E3
def test_accessibility_already_on_is_left_on(phone, monkeypatch):
    agent = use_agent(monkeypatch, Agent())
    agent.enabled = True
    with pytest.raises(RuntimeError):
        acc.app_launch({})
    assert agent.switches == []


# covers: delivery.acceptance/E3
def test_the_input_probe_is_stopped(phone, monkeypatch):
    use_agent(monkeypatch, Agent())
    phone.answers = {'rime-check': Result('ok', 0), 'test -x /usr/bin/rungic-input-probe': Result('', 0)}
    acc.rime_input({})
    assert phone.commands[-1] == 'pkill -f -x /usr/bin/rungic-input-probe'


# covers: delivery.acceptance/E3
def test_the_recording_is_deleted(phone, monkeypatch):
    use_agent(monkeypatch, Agent(nodes=[{'path': '/tile', 'extents': [0, 0, 100, 40]}]))
    path = '/home/u/Videos/screen-recording-1.mp4'
    probe = json.dumps({'streams': [{'codec_type': 'video'}, {'codec_type': 'audio'}], 'format': {'duration': '5.0'}})
    phone.answers = {'ffprobe': lambda script: Result(f'{path}\n{time.time()}\n{probe}')}
    row = acc.screen_recording({}, seconds=4)
    assert row['details']['file'] == path
    assert f'rm -f {path}' in phone.commands


class Screen:
    """kscreen-doctor and the Android host's display file of a stand-in phone."""

    def __init__(self, scale=3.0, refresh=120):
        self.scale, self.refresh = scale, refresh

    def answer(self, script):
        if script.startswith('kscreen-doctor -j'):
            return Result(json.dumps({'outputs': [{
                'name': 'Android-1', 'enabled': True, 'scale': self.scale, 'currentModeId': '1', 'vrrPolicy': 1,
                'modes': [{'id': '1', 'size': {'width': 1080, 'height': 2400}}]}]}))
        if script.startswith('kscreen-doctor '):
            for change in script.split()[1:]:
                if m := re.match(r'output\.[^.]+\.scale\.(.+)$', change):
                    self.scale = float(m[1])
                elif m := re.match(r'output\.[^.]+\.mode\.\d+x\d+@(\d+)$', change):
                    self.refresh = int(m[1])
                elif change.endswith('vrrpolicy.automatic'):
                    self.refresh = 0
            return Result()
        if 'android-display.json' in script:
            return Result(json.dumps({'refreshPolicy': self.refresh}))
        if script.startswith('wm size'):
            return Result('Physical size: 1080x2400')
        return Result()


# covers: delivery.acceptance/E3
def test_display_scale_and_refresh_policy_are_restored(phone, monkeypatch):
    screen = Screen()
    phone.answers = {'': screen.answer}
    row = acc.display_scale_roundtrip({})
    assert row['passed'] and row['details']['tried'] == 2.75
    assert screen.scale == 3.0
    row = acc.display_refresh_policy({})
    assert row['passed'], row
    assert screen.refresh == 120


# covers: delivery.acceptance/E6
def test_the_report_and_its_manual_items(tmp_path, monkeypatch):
    monkeypatch.setattr(acc, 'RESULTS', tmp_path / '.work/acceptance')
    monkeypatch.setattr(acc, 'bring_to_front', lambda: {'was_in_front': True, 'in_front': True})
    monkeypatch.setattr(acc, 'rungic_agent', types.SimpleNamespace(
        screenshot=lambda: (_ for _ in ()).throw(RuntimeError('no screen'))))
    monkeypatch.setitem(acc.CHECKS, 'fake_good', lambda ctx: acc.result(True, {'ms': 3}))
    monkeypatch.setitem(acc.CHECKS, 'fake_bad', lambda ctx: acc.result(False, error='no'))
    scenarios = [{'id': 'a.good', 'title': 'good', 'level': 'smoke', 'check': 'fake_good'},
                 {'id': 'a.bad', 'title': 'bad', 'level': 'smoke', 'check': 'fake_bad'}]
    report = acc.run_scenarios(scenarios, release='20261001.2')
    path = Path(report['path'])
    assert path.parent.parent == tmp_path / '.work/acceptance/20261001.2'
    assert re.fullmatch(r'\d{8}-\d{6}', path.parent.name) and path.name == 'report.json'
    saved = json.loads(path.read_text())
    assert saved['failed_ids'] == ['a.bad'] and saved['passed'] is False
    manual = ' '.join(saved['manual']).lower()
    for item in ('image quality', 'acoustic', 'synchronisation', 'pinyin', 'casting'):
        assert item in manual, item


def geometry_phone(phone, output, display):
    def answer(script):
        if script.startswith('kscreen-doctor -j'):
            return Result(json.dumps({'outputs': [{
                'name': 'WL-0', 'enabled': True, 'scale': 2, 'rotation': 1, 'currentModeId': '1',
                'modes': [{'id': '1', 'size': {'width': output[0], 'height': output[1]}, 'refreshRate': 120}]}]}))
        if 'android-display.json' in script:
            return Result(json.dumps(display))
        if script.startswith('wm size'):
            return Result('Physical size: 1008x2244')
        return Result()
    phone.answers = {'': answer}


# covers: delivery.acceptance/E3
def test_the_phone_output_may_use_the_render_size_the_app_chose(phone):
    # Without KGSL the app renders a 720 short edge for CPU rendering (husky); the output then
    # matches the app's render size, not the panel's native 1008x2244.
    geometry_phone(phone, (720, 1602), {'renderWidth': 720, 'renderHeight': 1602})
    row = acc.display_geometry({})
    assert row['passed'], row
    assert row['metrics']['refresh_hz'] == 120


# covers: delivery.acceptance/E3
def test_an_output_matching_neither_render_nor_panel_size_fails(phone):
    geometry_phone(phone, (800, 1600), {'renderWidth': 720, 'renderHeight': 1602})
    assert not acc.display_geometry({})['passed']
    # Without the app's display file the panel size is still the reference.
    geometry_phone(phone, (1008, 2244), {})
    assert acc.display_geometry({})['passed']
