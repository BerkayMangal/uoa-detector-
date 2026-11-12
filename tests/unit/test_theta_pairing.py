"""Single-use grant redemption and immutable per-connector job scope."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import select, update
from webapp.board.db import make_engine, session_factory
from webapp.theta_validation import pairing, service, storage

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
ENTRY = datetime(2026, 11, 11, 15, tzinfo=UTC)
SYMBOL = "TSLA261127C00410000"


@pytest.fixture
def engine(tmp_path):
    value = make_engine(f"sqlite:///{tmp_path / 'pairing.db'}")
    storage.ensure(value)
    pairing.ensure(value)
    yield value
    value.dispose()


def job(engine, minutes=0):
    return service.create_request(
        engine,
        service.RequestSpec(
            option_symbol=SYMBOL,
            planned_entry_at=ENTRY + timedelta(minutes=minutes),
        ),
        NOW,
    )


def test_pairing_snapshots_pending_jobs_and_stores_only_hashes(engine):
    request = job(engine)
    issued = pairing.issue(engine, NOW)
    assert len(issued.token) == 64
    assert issued.request_count == 1
    assert issued.expires_at == NOW + timedelta(hours=2)
    with session_factory(engine)() as session:
        grant = session.scalar(select(pairing.ThetaPairingGrant))
        assert grant.token_sha256 == hashlib.sha256(issued.token.encode()).hexdigest()
        assert issued.token not in grant.request_ids_json
        assert request["request_id"] in grant.request_ids_json
        stored_values = [getattr(grant, column.name) for column in grant.__table__.columns]
        assert issued.token not in stored_values
    assert pairing.authenticate(engine, issued.token, NOW) is None


def test_redeemed_connector_scope_never_expands_to_later_jobs(engine):
    original = job(engine)
    issued = pairing.issue(engine, NOW)
    later = job(engine, 1)
    connector = pairing.redeem(engine, issued.token, NOW + timedelta(seconds=1))
    assert connector.token != issued.token
    assert connector.request_count == 1
    assert connector.expires_at == NOW + timedelta(days=7, seconds=1)
    assert pairing.authenticate(engine, connector.token, NOW + timedelta(seconds=2)) == (
        original["request_id"],
    )
    assert later["request_id"] not in pairing.authenticate(
        engine, connector.token, NOW + timedelta(seconds=2)
    )
    with session_factory(engine)() as session:
        saved = session.scalar(select(pairing.ThetaConnectorSession))
        assert saved.token_sha256 == hashlib.sha256(connector.token.encode()).hexdigest()
        assert connector.token not in [
            getattr(saved, column.name) for column in saved.__table__.columns
        ]


def test_only_mature_pending_jobs_enter_the_scope(engine):
    request = service.create_request(
        engine,
        service.RequestSpec(
            option_symbol=SYMBOL,
            planned_entry_at=ENTRY,
        ),
        ENTRY + timedelta(minutes=1),
    )
    issued = pairing.issue(engine, ENTRY + timedelta(minutes=2))
    assert issued.request_count == 0
    connector = pairing.redeem(engine, issued.token, ENTRY + timedelta(minutes=3))
    assert pairing.authenticate(engine, connector.token, NOW) == ()
    assert request["request_id"] not in pairing.authenticate(engine, connector.token, NOW)


def test_scope_is_bounded_to_32_existing_requests(engine):
    for minute in range(40):
        job(engine, minute)
    grant = pairing.issue(engine, NOW)
    assert grant.request_count == 32
    session = pairing.redeem(engine, grant.token, NOW)
    assert len(pairing.authenticate(engine, session.token, NOW)) == 32


@pytest.mark.parametrize("raw", ["", "x" * 64, "A" * 64, "0" * 63, "0" * 65, "0" * 64])
def test_malformed_and_unknown_tokens_are_rejected(engine, raw):
    with pytest.raises(ValueError, match="invalid_pairing_token"):
        pairing.redeem(engine, raw, NOW)
    assert pairing.authenticate(engine, raw, NOW) is None


def test_grant_expiry_is_strict_and_grant_cannot_be_replayed(engine):
    grant = pairing.issue(engine, NOW)
    with pytest.raises(ValueError, match="pairing_token_expired"):
        pairing.redeem(engine, grant.token, grant.expires_at)
    fresh = pairing.issue(engine, NOW)
    pairing.redeem(engine, fresh.token, NOW)
    with pytest.raises(ValueError, match="pairing_token_consumed"):
        pairing.redeem(engine, fresh.token, NOW)


def test_connector_lives_after_grant_expiry_but_not_after_its_own_expiry(engine):
    request = job(engine)
    grant = pairing.issue(engine, NOW)
    connector = pairing.redeem(engine, grant.token, NOW)
    assert pairing.authenticate(engine, connector.token, NOW + timedelta(hours=3)) == (
        request["request_id"],
    )
    assert pairing.authenticate(engine, connector.token, connector.expires_at) is None
    assert pairing.authenticate(engine, connector.token, NOW - timedelta(seconds=1)) is None


def test_concurrent_redemption_mints_exactly_one_session(engine):
    job(engine)
    grant = pairing.issue(engine, NOW)
    barrier = Barrier(6)

    def redeem_once(_):
        barrier.wait()
        try:
            return pairing.redeem(engine, grant.token, NOW).token
        except ValueError as error:
            assert str(error) == "pairing_token_consumed"
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(redeem_once, range(6)))
    assert len([value for value in results if value]) == 1
    with session_factory(engine)() as session:
        assert len(list(session.scalars(select(pairing.ThetaConnectorSession)))) == 1


def test_corrupt_scope_fails_closed(engine):
    grant = pairing.issue(engine, NOW)
    connector = pairing.redeem(engine, grant.token, NOW)
    with engine.begin() as connection:
        connection.execute(
            update(pairing.ThetaPairingGrant).values(request_ids_json='["arbitrary-global-access"]')
        )
    assert pairing.authenticate(engine, connector.token, NOW) is None


def test_naive_clock_is_never_accepted(engine):
    with pytest.raises(ValueError, match="aware_pairing_clock_required"):
        pairing.issue(engine, NOW.replace(tzinfo=None))


def test_personalized_source_has_one_scoped_token_and_pinned_origin():
    source = 'EMBEDDED_TOKEN = ""\nEMBEDDED_UOA = ""\nprint("no account password")\n'
    token = "a" * 64
    rendered = pairing.personalize_source(source, token, "https://uoa.example.test/")
    namespace = {}
    exec(compile(rendered.split("print(")[0], "<connector-header>", "exec"), namespace)
    assert namespace["EMBEDDED_TOKEN"] == token
    assert namespace["EMBEDDED_UOA"] == "https://uoa.example.test"
    assert rendered.count(token) == 1
    assert rendered.count("https://uoa.example.test") == 1


@pytest.mark.parametrize(
    "source",
    [
        'EMBEDDED_TOKEN = ""\n',
        'EMBEDDED_TOKEN = ""\nEMBEDDED_TOKEN = ""\nEMBEDDED_UOA = ""\n',
        ' EMBEDDED_TOKEN = ""\nEMBEDDED_UOA = ""\n',
    ],
)
def test_source_marker_mismatch_is_rejected(source):
    with pytest.raises(ValueError, match="connector_source_marker_mismatch"):
        pairing.personalize_source(source, "a" * 64, "https://uoa.example.test")


@pytest.mark.parametrize(
    "origin",
    [
        "http://uoa.example.test",
        "https://user:password@uoa.example.test",
        "https://uoa.example.test/path",
        "https://uoa.example.test?token=1",
        "https://uoa.example.test#fragment",
        "https://uoa.example.test\n",
    ],
)
def test_untrusted_origin_shapes_are_rejected(origin):
    with pytest.raises(ValueError, match="invalid_connector_origin"):
        pairing.personalize_source('EMBEDDED_TOKEN = ""\nEMBEDDED_UOA = ""\n', "a" * 64, origin)
