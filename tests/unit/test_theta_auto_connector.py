"""Continuous device reconnect and installation use only synthetic local fixtures."""
from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import plistlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from scripts import theta_auto_connector as connector
from scripts import theta_mac_connector as legacy
from webapp.board.db import make_engine
from webapp.theta_validation import pairing, service, storage

from tests.unit.test_theta_mac_connector import CLOSE, START, SYMBOL, FakeOpener, packet, payload

REQUEST_ID = "theta_" + "a" * 32
TOKEN = "b" * 64
BASE = "https://uoa.example.test"
JOB = {"request_id": REQUEST_ID, "request_hash": "c" * 64,
       "option_symbol": SYMBOL, "planned_entry_at": START.isoformat(),
       "session_close_at": CLOSE.isoformat()}


class FakeTransport:
    def __init__(self):
        self.events = []
        self.pending = []
        self.terminal_error = None
        self.window_error = None
        self.upload_error = None

    def check_terminal(self):
        self.events.append(('terminal_check',))
        if self.terminal_error:
            raise connector.ConnectorError(self.terminal_error)

    def heartbeat(self, state, error, count):
        self.events.append(('heartbeat', state, error, count))

    def jobs(self):
        self.events.append(('jobs',))
        return self.pending

    def window(self, contract, start, label):
        self.events.append(('window', label, start))
        if self.window_error:
            raise connector.ConnectorError(self.window_error)
        return packet(payload((start + timedelta(seconds=3)).isoformat()), start, label)

    def upload_raw(self, request_id, body):
        self.events.append(('upload', request_id, body))
        if self.upload_error:
            raise connector.ConnectorError(self.upload_error)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(connector.Path, 'home', classmethod(lambda cls: tmp_path))
    monkeypatch.delenv('UOA_BASE_URL', raising=False)
    monkeypatch.delenv('THETA_TERMINAL_URL', raising=False)
    return tmp_path


def test_standalone_python39_and_frozen_native_helper_behavior():
    tree = ast.parse(Path(connector.__file__).read_text(), feature_version=(3, 9))
    imports = {(node.module or '').split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imports |= {item.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for item in node.names}
    assert imports <= {'__future__', 'argparse', 'base64', 'fcntl', 'hashlib', 'json', 'os',
                       'plistlib', 'random', 're', 'signal', 'socket', 'ssl', 'stat', 'subprocess',
                       'sys', 'tempfile', 'time', 'contextlib', 'dataclasses', 'datetime', 'decimal',
                       'http', 'pathlib', 'typing', 'urllib', 'zoneinfo'}
    prior = ast.parse(Path(legacy.__file__).read_text())
    for name in ('origin', 'native_time', 'first_quote', 'Contract', 'Transport', 'request_timeout', 'encode_json'):
        old = next(node for node in prior.body if getattr(node, 'name', None) == name)
        new = next(node for node in tree.body if getattr(node, 'name', None) == name)
        assert ast.dump(old) == ast.dump(new), name


def test_personalized_device_source_compiles_without_repo_imports():
    source = pairing.personalize_source(Path(connector.__file__).read_text(), TOKEN, BASE)
    assert sum(line.startswith('EMBEDDED_TOKEN = ') for line in source.splitlines()) == 1
    assert sum(line.startswith('EMBEDDED_UOA = ') for line in source.splitlines()) == 1
    compile(source, 'download.py', 'exec')
    assert 'getpass' not in source


@pytest.mark.parametrize('terminal,expected', [(True, 'theta_authentication_failed'), (False, 'uoa_authentication_failed')])
def test_local_and_remote_authentication_are_distinguished(terminal, expected):
    client = connector.AutoTransport(BASE, 'http://127.0.0.1:25503', bearer_token=TOKEN)
    failed = FakeOpener(error=HTTPError('private-url', 401, 'private-body', {}, io.BytesIO()))
    client._local = failed
    client._remote = failed
    with pytest.raises(connector.ConnectorError, match=expected):
        client._request((client.terminal if terminal else client.base) + '/probe', terminal=terminal)
    sent = failed.calls[0][0]
    assert ('Authorization' in sent.headers) is (not terminal)


def test_device_paths_and_validated_upload_acknowledgment():
    client = connector.AutoTransport(BASE, 'http://127.0.0.1:25503', bearer_token=TOKEN)
    client._remote = FakeOpener(body=b'{"jobs":[]}')
    assert client.jobs() == []
    assert client._remote.calls[0][0].full_url == BASE + '/api/theta/device/jobs?limit=4'
    client._remote = FakeOpener(body=json.dumps({'request_id': REQUEST_ID, 'receipt_id': 'd'*64}).encode())
    client.upload_raw(REQUEST_ID, b'{}')
    assert client._remote.calls[0][0].full_url == BASE + '/api/theta/device/results/' + REQUEST_ID
    client._remote = FakeOpener(body=b'{"accepted":true}')
    with pytest.raises(connector.ConnectorError, match='upload_acknowledgment_invalid'):
        client.upload_raw(REQUEST_ID, b'{}')


def test_terminal_down_reports_health_without_jobs_or_attempts(tmp_path):
    client = FakeTransport()
    client.terminal_error = 'terminal_unreachable'
    client.pending = [JOB]
    outbox = connector.Outbox(tmp_path / 'outbox')
    for _ in range(5):
        assert connector.cycle(client, outbox) is False
    assert not outbox.files()
    assert sum(e[0] == 'heartbeat' for e in client.events) == 5
    assert not any(e[0] in ('jobs', 'window', 'upload') for e in client.events)


@pytest.mark.parametrize('failure,state,error', [
    ('terminal_unreachable', 'unreachable', 'terminal_unreachable'),
    ('theta_authentication_failed', 'authentication_failed', 'authentication_failed'),
])
def test_mid_job_transport_failure_does_not_consume_data_attempt(tmp_path, failure, state, error):
    client = FakeTransport()
    client.pending = [JOB]
    client.window_error = failure
    outbox = connector.Outbox(tmp_path / 'outbox')
    assert connector.cycle(client, outbox) is False
    assert client.events[-1] == ('heartbeat', state, error, 0)
    assert not outbox.files() and not any(e[0] == 'upload' for e in client.events)


def test_valid_native_windows_are_preserved_and_exits_follow_selected_entry(tmp_path):
    client = FakeTransport()
    client.pending = [JOB]
    outbox = connector.Outbox(tmp_path / 'outbox')
    assert connector.cycle(client, outbox)
    windows = [e for e in client.events if e[0] == 'window']
    assert windows == [('window', 'entry', START),
                       ('window', 'm15', START+timedelta(seconds=3, minutes=15)),
                       ('window', 'm60', START+timedelta(seconds=3, minutes=60))]
    uploads = [e for e in client.events if e[0] == 'upload']
    assert len(uploads) == 1
    body = json.loads(uploads[0][2])
    assert body['status'] == 'ok' and len(body['packets']) == 3
    assert body['packets'][0]['requested_interval'] == 'tick'
    assert not outbox.files()


def test_invalid_native_payload_is_a_bounded_data_attempt():
    client = FakeTransport()
    client.window_error = 'unsupported_payload'
    request_id, body = connector.collect_job(client, JOB)
    assert request_id == REQUEST_ID
    assert json.loads(body)['status'] == 'unsupported_payload'
    assert json.loads(body)['packets'] == []


def test_upload_replay_survives_restart_with_identical_body_and_source_times(tmp_path):
    client = FakeTransport()
    client.upload_error = 'request_failed'
    directory = tmp_path / 'outbox'
    outbox = connector.Outbox(directory)
    body = b'{"attempted_at":"2026-11-10T00:00:00Z","packets":[{"received_at":"2026-11-09T18:00:00Z"}]}'
    path = outbox.save(REQUEST_ID, body)
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(connector.ConnectorError, match='request_failed'):
        outbox.flush(client)
    assert path.read_bytes() == body
    client.upload_error = None
    connector.Outbox(directory).flush(client)
    sent = [e[2] for e in client.events if e[0] == 'upload']
    assert sent == [body, body] and not path.exists()


@pytest.mark.parametrize('code,reason', [
    (409, 'completed_result_is_immutable'), (409, 'attempt_limit_reached'),
    (422, 'invalid_theta_upload'), (404, 'request_not_found'),
])
def test_definite_structured_refusals_preserve_bytes_and_allow_next_job(home, code, reason):
    client = connector.AutoTransport(BASE, 'http://127.0.0.1:25503', bearer_token=TOKEN)
    error = HTTPError('private-url', code, 'private-body', {}, io.BytesIO(json.dumps({'detail': reason}).encode()))
    client._remote = FakeOpener(error=error)
    outbox = connector.Outbox(home/'outbox')
    body = b'{"original":"retained"}'
    original = outbox.save(REQUEST_ID, body)
    assert outbox.flush(client) == 1
    assert not original.exists()
    rejected = outbox.rejected_files()[0]
    assert rejected.read_bytes() == body
    assert rejected.with_suffix('.reason').read_text() == reason
    next_client = FakeTransport()
    next_client.pending = [{**JOB, 'request_id': 'theta_'+'d'*32}]
    assert connector.cycle(next_client, outbox)
    assert any(e[0] == 'upload' and e[1] == 'theta_'+'d'*32 for e in next_client.events)
    assert ('heartbeat', 'reachable', 'upload_failed', 0) in next_client.events


@pytest.mark.parametrize('code,body', [
    (409, b'{"detail":"concurrent_receipt_retry_required"}'),
    (409, b'{"detail":"unrecognized"}'), (422, b'invalid JSON'),
    (422, b'['*1100 + b']'*1100),
    (422, b'{"detail":"attempt_time_in_future"}'), (503, b'{"detail":"invalid_theta_upload"}'),
])
def test_ambiguous_or_retryable_refusals_remain_in_outbox(home, code, body):
    client = connector.AutoTransport(BASE, 'http://127.0.0.1:25503', bearer_token=TOKEN)
    client._remote = FakeOpener(error=HTTPError('private-url', code, 'private-body', {}, io.BytesIO(body)))
    outbox = connector.Outbox(home/'outbox')
    original = outbox.save(REQUEST_ID, b'{}')
    with pytest.raises(connector.ConnectorError, match='request_failed'):
        outbox.flush(client)
    assert original.read_bytes() == b'{}' and not outbox.rejected_files()


def test_full_retained_rejections_never_delete_pending_evidence(home):
    outbox = connector.Outbox(home/'outbox')
    for index in range(32):
        body = str(index).encode()
        path = outbox.save(REQUEST_ID, body)
        outbox.retain_rejection(path, body, 'invalid_theta_upload')
    path = outbox.save(REQUEST_ID, b'one more')
    with pytest.raises(connector.ConnectorError, match='outbox_full'):
        outbox.retain_rejection(path, b'one more', 'invalid_theta_upload')
    assert path.read_bytes() == b'one more'


def test_three_rejected_quote_attempts_advance_real_queue_without_fabricated_prices(home, monkeypatch):
    engine = make_engine(f"sqlite:///{home/'queue.db'}")
    storage.ensure(engine)
    now = datetime.now(UTC)
    bad = service.create_request(engine, service.RequestSpec(option_symbol=SYMBOL, planned_entry_at=START), now)
    good = service.create_request(engine, service.RequestSpec(option_symbol=SYMBOL, planned_entry_at=START+timedelta(minutes=2)), now)

    class RealQueue(FakeTransport):
        def jobs(self):
            return storage.pending(engine, datetime.now(UTC), 1)

        def upload_raw(self, request_id, body):
            self.events.append(('upload', request_id, body))
            try:
                service.ingest(engine, request_id, service.UploadSpec.model_validate_json(body), datetime.now(UTC))
            except ValueError as error:
                raise connector.UploadRejectedError(str(error), 422) from None

    original_collect = connector.collect_job

    def invalid_exit_packet(client, job):
        request_id, body = original_collect(client, job)
        if request_id == bad['request_id']:
            damaged = json.loads(body)
            damaged['packets'] = damaged['packets'][:1]
            body = connector.encode_json(damaged)
        return request_id, body

    monkeypatch.setattr(connector, 'collect_job', invalid_exit_packet)
    client = RealQueue()
    outbox = connector.Outbox(home/'outbox')
    for _ in range(4):
        assert connector.cycle(client, outbox)
    view = storage.view(engine, now=datetime.now(UTC))
    jobs = {row['request_id']: row for row in view['jobs']}
    assert jobs[bad['request_id']]['state'] == 'retry_limit'
    assert jobs[bad['request_id']]['attempts'] == 3
    assert jobs[good['request_id']]['state'] == 'completed'
    rejected = [json.loads(path.read_bytes()) for path in outbox.rejected_files()]
    failures = [json.loads(e[2]) for e in client.events if e[0] == 'upload'
                and json.loads(e[2])['status'] == 'unsupported_payload']
    assert len(rejected) == len(failures) == 3
    assert all(f['packets'] == [] for f in failures)
    assert {(x['request_hash'], x['attempted_at']) for x in rejected} == {
        (x['request_hash'], x['attempted_at']) for x in failures}
    failed_results = [r for r in view['recent_results'] if r['request_id'] == bad['request_id']]
    assert all(r['result']['status'] == 'UNKNOWN' for r in failed_results)
    assert not outbox.files()
    engine.dispose()


def test_rejected_failure_receipt_never_creates_another_failure(home):
    client = FakeTransport()

    def reject(*_):
        raise connector.UploadRejectedError('invalid_theta_upload', 422)

    client.upload_raw = reject
    outbox = connector.Outbox(home/'outbox')
    body = connector.encode_json({'request_hash': 'c'*64, 'status': 'unsupported_payload', 'packets': []})
    outbox.save(REQUEST_ID, body)
    assert outbox.flush(client) == 1
    assert not outbox.files() and len(outbox.rejected_files()) == 1


def test_pending_upload_replays_even_when_terminal_is_down(tmp_path):
    client = FakeTransport()
    client.terminal_error = 'terminal_unreachable'
    outbox = connector.Outbox(tmp_path / 'outbox')
    outbox.save(REQUEST_ID, b'{}')
    assert not connector.cycle(client, outbox)
    assert next(e for e in client.events if e[0] != 'heartbeat')[0] == 'upload'
    assert not outbox.files()


def test_outbox_detects_modified_bytes_before_upload(tmp_path):
    outbox = connector.Outbox(tmp_path / 'outbox')
    path = outbox.save(REQUEST_ID, b'{}')
    path.write_bytes(b'{"changed":true}')
    client = FakeTransport()
    with pytest.raises(connector.ConnectorError, match='outbox_invalid'):
        outbox.flush(client)
    assert not client.events


def test_outbox_file_count_and_body_caps(tmp_path):
    outbox = connector.Outbox(tmp_path / 'outbox')
    for i in range(connector.MAX_OUTBOX_FILES):
        outbox.save(REQUEST_ID, str(i).encode())
    with pytest.raises(connector.ConnectorError, match='outbox_full'):
        outbox.save(REQUEST_ID, b'extra')
    with pytest.raises(connector.ConnectorError, match='invalid_request'):
        outbox.save(REQUEST_ID, b'x'*(connector.MAX_BYTES+1))


def test_reconnect_survives_transient_errors_and_stops_on_revocation(home, monkeypatch):
    calls, sleeps = [], []
    results = ['request_failed', False, True, 'uoa_authentication_failed']

    def attempt(*_):
        result = results[len(calls)]
        calls.append(result)
        if isinstance(result, str):
            raise connector.ConnectorError(result)
        return result

    monkeypatch.setattr(connector, 'cycle', attempt)
    monkeypatch.setattr(connector.time, 'sleep', sleeps.append)
    monkeypatch.setattr(connector.random, 'uniform', lambda *_: 0)
    assert connector.run_loop(FakeTransport(), connector.Outbox(home/'outbox')) == 0
    assert sleeps == [60, 60, 60] and len(calls) == 4
    assert [connector.reconnect_delay(x) for x in (1, 2, 3, 4, 100000)] == [60, 120, 240, 300, 300]


def test_pinned_configuration_never_prompts_or_uses_owner_password(home, monkeypatch):
    monkeypatch.setattr(connector, 'EMBEDDED_TOKEN', TOKEN)
    monkeypatch.setattr(connector, 'EMBEDDED_UOA', BASE)
    assert connector.configuration(home) == (BASE, connector.DEFAULT_TERMINAL, TOKEN)
    monkeypatch.setenv('UOA_BASE_URL', 'https://other.example.test')
    with pytest.raises(connector.ConnectorError, match='invalid_request'):
        connector.configuration(home)


def test_installed_secret_is_private_and_matches_pinned_origin(home, monkeypatch):
    monkeypatch.setattr(connector, 'EMBEDDED_UOA', BASE)
    connector.private_write(home/'device.json', json.dumps({'base_url': BASE, 'terminal_url': connector.DEFAULT_TERMINAL, 'token': TOKEN}).encode())
    assert connector.configuration(home) == (BASE, connector.DEFAULT_TERMINAL, TOKEN)
    (home/'device.json').chmod(0o644)
    with pytest.raises(connector.ConnectorError, match='unsafe_local_file'):
        connector.configuration(home)


def test_personalized_check_mode_makes_download_private_before_network(home, monkeypatch):
    source = home/'download.py'
    source.write_text('synthetic source')
    source.chmod(0o644)
    monkeypatch.setattr(connector, '__file__', str(source))
    monkeypatch.setattr(connector, 'EMBEDDED_TOKEN', TOKEN)
    monkeypatch.setattr(connector, 'EMBEDDED_UOA', BASE)
    client = FakeTransport()
    monkeypatch.setattr(connector, 'AutoTransport', lambda *a, **k: client)
    assert connector.auto_main(['--check']) == 0
    assert source.stat().st_mode & 0o777 == 0o600


def test_install_creates_only_private_user_files_and_supervised_agent(home, monkeypatch):
    source = home / 'download with spaces.py'
    source.write_text(pairing.personalize_source(Path(connector.__file__).read_text(), TOKEN, BASE))
    monkeypatch.setattr(connector, '__file__', str(source))
    monkeypatch.setattr(connector.sys, 'platform', 'darwin')
    monkeypatch.setattr(connector.sys, 'executable', '/opt/python with spaces/bin/python3')
    commands = []
    monkeypatch.setattr(connector.subprocess, 'run', lambda args, **kwargs: commands.append((args, kwargs)) or SimpleNamespace(returncode=0))
    directory = connector.app_directory()
    connector.install(directory, BASE, connector.DEFAULT_TERMINAL, TOKEN)
    config = directory/'device.json'
    assert config.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    assert json.loads(config.read_text())['token'] == TOKEN
    installed = (directory/'connector.py').read_text()
    assert TOKEN not in installed and 'EMBEDDED_TOKEN = ""' in installed
    assert source.stat().st_mode & 0o777 == 0o600
    agent = home/'Library'/'LaunchAgents'/(connector.AGENT_LABEL+'.plist')
    plist = plistlib.loads(agent.read_bytes())
    assert plist['RunAtLoad'] and plist['KeepAlive'] == {'SuccessfulExit': False}
    assert plist['ProgramArguments'] == ['/opt/python with spaces/bin/python3', str(directory/'connector.py'), '--watch']
    assert TOKEN not in agent.read_text()
    assert commands[1][0][1] == 'bootstrap'
    assert all(call[1]['timeout'] == 20 and call[1]['capture_output'] for call in commands)


def test_install_refuses_non_mac_and_symlink_paths(home, monkeypatch):
    monkeypatch.setattr(connector.sys, 'platform', 'linux')
    with pytest.raises(connector.ConnectorError, match='mac_required_for_install'):
        connector.install(connector.app_directory(), BASE, connector.DEFAULT_TERMINAL, TOKEN)
    target = home/'target'
    target.mkdir()
    symlink = home/'unsafe'
    symlink.symlink_to(target)
    with pytest.raises(connector.ConnectorError, match='unsafe_local_path'):
        connector.private_directory(symlink)


def test_uninstall_leaves_unrelated_files_and_theta_installation(home, monkeypatch):
    directory = connector.app_directory()
    connector.private_directory(directory)
    connector.private_write(directory/'device.json', b'{}')
    unrelated = directory/'user-notes.txt'
    unrelated.write_text('Keep this file')
    theta = home/'ThetaTerminal.jar'
    theta.write_text('not ours')
    monkeypatch.setattr(connector.sys, 'platform', 'darwin')
    monkeypatch.setattr(connector, 'launchctl', lambda *_a, **_k: None)
    connector.uninstall(directory)
    assert unrelated.read_text() == 'Keep this file' and theta.read_text() == 'not ours'
    assert not (directory/'device.json').exists()


def test_safe_log_rotation_is_bounded_and_private(home):
    directory = connector.app_directory()
    connector.private_directory(directory)
    connector.private_write(directory/'connector.log', b'x'*(256*1024))
    connector.report('terminal_unreachable', error=True)
    assert (directory/'connector.log.1').stat().st_size == 256*1024
    assert (directory/'connector.log').stat().st_mode & 0o777 == 0o600
    assert 'terminal_unreachable' in (directory/'connector.log').read_text()


def test_spool_hash_is_bound_to_exact_bytes(tmp_path):
    body = b'{"x":1}'
    path = connector.Outbox(tmp_path/'outbox').save(REQUEST_ID, body)
    assert path.name == REQUEST_ID + '_' + hashlib.sha256(body).hexdigest() + '.json'
    assert os.stat(path).st_uid == os.getuid()
