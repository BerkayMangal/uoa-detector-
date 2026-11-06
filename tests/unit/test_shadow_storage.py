"""Durable claims and physical receipts never manufacture trusted quote evidence."""
import gzip
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import event, func, select, update
from webapp.board.db import make_engine, session_factory
from webapp.shadow_capture import config, storage
from webapp.shadow_capture.model import digest

T = datetime(2026, 11, 6, 15, tzinfo=UTC)
DAY = T.date()
IDENTITY = {'version':'shadow_price_v1','profile_hash':'a'*64,
            'implementation_hash':'b'*64,'live_profile_hash':'c'*64,
            'news_profile_hash':'d'*64,'news_implementation_hash':'e'*64}


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'identity', lambda: dict(IDENTITY))
    e = make_engine(f"sqlite:///{tmp_path / 'shadow.db'}")
    storage.ensure(e)
    yield e
    e.dispose()


def put(engine, key='first', **changes):
    args = dict(key=key,kind='candidate',at=T,payload={'value':100},day=DAY,
                clock=lambda:T+timedelta(seconds=1)) | changes
    return storage.put(engine, **args)


def test_physical_confirmation_happens_after_parent_commit(engine):
    calls = []
    def clock():
        with session_factory(engine)() as s:
            assert s.scalar(select(func.count()).select_from(storage.ShadowEvent)) == 1
            assert s.scalar(select(func.count()).select_from(storage.ShadowReceipt)) == len(calls)
        calls.append(True)
        return T+timedelta(seconds=len(calls))
    value = put(engine, clock=clock)
    assert value.at == T and value.confirmed_at == T+timedelta(seconds=2)
    assert storage.get(engine,value.key,T+timedelta(seconds=1)) is None
    assert storage.get(engine,value.key,value.confirmed_at) == value


def test_duplicate_key_never_rewrites_first_source_or_confirmation(engine):
    first = put(engine)
    assert put(engine,payload={'value':999},clock=lambda:pytest.fail('duplicate confirmed later')) is None
    assert storage.get(engine,'first',T+timedelta(days=1)) == first
    assert storage.count(engine,'candidate',T+timedelta(days=1)) == 1


def test_unique_owner_and_independent_nullable_owners(engine):
    assert put(engine,'a',unique_owner='ticker:NVDA')
    assert put(engine,'b',unique_owner='ticker:NVDA') is None
    assert put(engine,'c') and put(engine,'d')
    assert storage.count(engine,'candidate',T+timedelta(seconds=2)) == 3


def test_concurrent_slot_and_ticker_claims_bound_four_daily_candidates(engine):
    def admit(i):
        for slot in range(1,5):
            if put(engine,digest(('slot',DAY.isoformat(),slot)),
                   unique_owner=digest(('ticker',DAY.isoformat(),i%8)),
                   payload={'ticker':i%8,'slot':slot}) is not None:
                return True
        return False
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(admit,range(32)))
    assert sum(results) == 4
    rows = storage.list_events(engine,'candidate',T+timedelta(seconds=2),day=DAY)
    assert len(rows) == 4 and len({r.unique_owner for r in rows}) == 4


def test_missing_receipt_is_not_recreated_for_old_candidate_or_packet(engine):
    def crash():
        raise RuntimeError('post-commit interruption')
    for kind in ('candidate','packet','outcome'):
        with pytest.raises(RuntimeError,match='interruption'):
            put(engine,kind,kind=kind,clock=crash)
        assert storage.get(engine,kind,T+timedelta(days=1)) is None
        raw = storage.get(engine,kind,T+timedelta(days=1),require_receipt=False)
        assert raw and raw.confirmed_at is None
        assert put(engine,kind,kind=kind) is None
    assert storage.count(engine,'candidate',T+timedelta(days=1)) == 1
    assert storage.list_events(engine,'candidate',T+timedelta(days=1)) == ()


def test_activation_recovery_uses_new_actual_confirmation_boundary(engine):
    clocks = iter((T,T-timedelta(seconds=1)))
    with pytest.raises(ValueError,match='receipt_before_event'):
        storage.activate(engine,lambda:next(clocks))
    assert storage.active(engine,T+timedelta(seconds=4)) is None
    fixed = storage.activate(engine,lambda:T+timedelta(seconds=5))
    assert fixed.at == T and fixed.confirmed_at == T+timedelta(seconds=5)
    assert storage.active(engine,T+timedelta(seconds=4)) is None
    assert storage.active(engine,T+timedelta(seconds=5)) == fixed
    assert storage.activate(engine,lambda:T+timedelta(days=1)) == fixed


def test_current_activation_requires_exact_live_news_and_implementation_identity(engine,monkeypatch):
    first = storage.activate(engine,lambda:T)
    monkeypatch.setattr(config,'identity',lambda:dict(IDENTITY)|{'live_profile_hash':'f'*64})
    assert storage.active(engine,T+timedelta(seconds=1)) is None
    later = storage.activate(engine,lambda:T+timedelta(seconds=1))
    assert later.key != first.key and later.confirmed_at > first.confirmed_at
    assert storage.active(engine,T+timedelta(seconds=1)) == later


def test_raw_blob_roundtrip_is_exact_and_private(engine):
    body = b'{"data":{"last_trade":{"price":100,"time":1793977200000}}}'
    value = put(engine,kind='packet',blob=body,payload={'body_bytes':len(body)})
    assert value.blob == body
    with session_factory(engine)() as s:
        raw = s.get(storage.ShadowEvent,'first')
        assert raw.blob != body and gzip.decompress(raw.blob) == body
    assert storage.get(engine,'first',T+timedelta(seconds=1)).blob == body


@pytest.mark.parametrize('field,value',[
    ('payload_json','{"value":999}'),('kind','outcome'),('at',T+timedelta(seconds=1)),
    ('day',date(2026,11,5)),('parent_key','different'),('unique_owner','different'),
    ('blob',gzip.compress(b'changed')),('blob',b'invalid gzip'),('checksum','0'*64),
])
def test_event_metadata_or_body_tampering_refuses_evidence(engine,field,value):
    put(engine,kind='packet',blob=b'original')
    with engine.begin() as c:
        c.execute(update(storage.ShadowEvent).where(storage.ShadowEvent.key=='first').values(**{field:value}))
    assert storage.get(engine,'first',T+timedelta(seconds=20)) is None


@pytest.mark.parametrize('stage',['recorded','confirmed'])
@pytest.mark.parametrize('field,value',[
    ('event_checksum','0'*64),('checksum','0'*64),('parent_key','different'),
    ('at',T+timedelta(seconds=2)),('key','different'),('stage','other'),
])
def test_receipt_tampering_keeps_valid_raw_ownership_but_never_confirmation(engine,stage,field,value):
    put(engine)
    with engine.begin() as c:
        c.execute(update(storage.ShadowReceipt).where(storage.ShadowReceipt.stage==stage).values(**{field:value}))
    assert storage.get(engine,'first',T+timedelta(seconds=20)) is None
    assert storage.get(engine,'first',T+timedelta(seconds=20),require_receipt=False).confirmed_at is None


def test_coherently_rehashed_receipt_before_event_still_refuses(engine):
    value = put(engine)
    bad = T-timedelta(seconds=1)
    with engine.begin() as c:
        c.execute(update(storage.ShadowReceipt).where(storage.ShadowReceipt.stage=='recorded').values(at=bad,
            checksum=digest(('receipt','recorded',value.key,value.checksum,bad.isoformat()))))
    assert storage.get(engine,value.key,T+timedelta(seconds=2)) is None


def test_future_parents_and_receipts_are_unavailable_asof(engine):
    put(engine,'future',at=T+timedelta(seconds=5),clock=lambda:T+timedelta(seconds=6))
    put(engine,'later-receipt',clock=lambda:T+timedelta(seconds=8))
    assert storage.get(engine,'future',T+timedelta(seconds=4)) is None
    assert storage.get(engine,'later-receipt',T+timedelta(seconds=7)) is None
    raw = storage.get(engine,'later-receipt',T+timedelta(seconds=7),require_receipt=False)
    assert raw and raw.confirmed_at is None
    assert storage.count(engine,'candidate',T+timedelta(seconds=4)) == 1
    assert len(storage.list_events(engine,'candidate',T+timedelta(seconds=8))) == 2


def test_bounded_inflate_refuses_even_coherently_rehashed_oversized_blob(engine):
    put(engine,kind='packet',blob=b'old')
    body = b'x'*(config.settings().max_body_bytes+1)
    with engine.begin() as c:
        c.execute(update(storage.ShadowEvent).values(blob=gzip.compress(body),
            checksum=storage._event_checksum('first','packet',T,DAY,None,None,'{"value":100}',body)))
    assert storage.get(engine,'first',T+timedelta(seconds=2),require_receipt=False) is None


def test_duplicate_json_keys_remain_invalid_after_rehash(engine):
    put(engine)
    body = '{"value":100,"value":999}'
    with engine.begin() as c:
        c.execute(update(storage.ShadowEvent).values(payload_json=body,
            checksum=storage._event_checksum('first','candidate',T,DAY,None,None,body,None)))
    assert storage.get(engine,'first',T+timedelta(seconds=2),require_receipt=False) is None


@pytest.mark.parametrize('changes',[
    {'at':T.replace(tzinfo=None)}, {'payload':{'value':float('nan')}},
    {'payload':{'value':float('inf')}}, {'blob':b'x'*65537}, {'blob':'not bytes'},
    {'day':T}, {'kind':'unknown'}, {'unique_owner':123}, {'key':''},
])
def test_invalid_inputs_refuse_before_sql(engine,changes):
    sql = []
    event.listen(engine,'before_cursor_execute',lambda _c,_cur,stmt,*_a:sql.append(stmt))
    with pytest.raises((ValueError,TypeError)):
        put(engine,**changes)
    assert not sql


@pytest.mark.parametrize('limit',[0,-1,True,130])
def test_invalid_read_limits_refuse(engine,limit):
    with pytest.raises(ValueError,match='read_limit'):
        storage.list_events(engine,'candidate',T,limit=limit)


def test_reads_filter_order_scope_and_never_initialize_or_mutate(engine,tmp_path):
    put(engine,'b',at=T+timedelta(seconds=2),parent_key='parent',clock=lambda:T+timedelta(seconds=3))
    put(engine,'a',parent_key='parent')
    put(engine,'other',kind='target',parent_key='parent')
    put(engine,'another-parent',parent_key='other')
    writes = []
    event.listen(engine,'before_cursor_execute',lambda _c,_cur,sql,*_a:writes.append(sql) if sql.lstrip().upper().startswith(('CREATE','ALTER','DROP','INSERT','UPDATE','DELETE')) else None)
    assert [v.key for v in storage.list_events(engine,'candidate',T+timedelta(seconds=3),day=DAY,parent_key='parent')] == ['a','b']
    assert storage.available(engine)
    assert storage.count(engine,'candidate',T+timedelta(seconds=3),day=DAY) == 3
    assert storage.get(engine,'a',T+timedelta(seconds=3))
    assert not writes
    empty = make_engine(f"sqlite:///{tmp_path/'empty.db'}")
    assert not storage.available(empty) and storage.active(empty,T) is None
    empty.dispose()


def test_all_external_read_clocks_must_be_aware(engine):
    at = T.replace(tzinfo=None)
    for read in (lambda:storage.get(engine,'missing',at),
                 lambda:storage.list_events(engine,'candidate',at),
                 lambda:storage.count(engine,'candidate',at),lambda:storage.active(engine,at),
                 lambda:storage.activate(engine,lambda:at)):
        with pytest.raises(ValueError,match='aware_clock'):
            read()


def test_first_receipt_commit_delay_moves_confirmed_availability_forward(engine):
    commits = []
    current = [T]
    def committed(_connection):
        commits.append(True)
        if len(commits) == 2:
            current[0] = T + timedelta(seconds=120)
    event.listen(engine,'commit',committed)
    value = put(engine,kind='packet',blob=b'wire',clock=lambda:current[0])
    assert len(commits) == 3
    assert value.confirmed_at == T + timedelta(seconds=120)
    assert storage.get(engine,value.key,T + timedelta(seconds=60)) is None
    with session_factory(engine)() as s:
        rows = {r.stage:r for r in s.scalars(select(storage.ShadowReceipt))}
    assert rows['recorded'].at.replace(tzinfo=UTC) == T
    assert rows['confirmed'].at.replace(tzinfo=UTC) == value.confirmed_at


def test_only_activation_can_recover_missing_second_stage(engine):
    def clock_values(values):
        iterator = iter(values)
        def clock():
            at = next(iterator)
            if at is None:
                raise RuntimeError('interrupted after first receipt')
            return at
        return clock
    with pytest.raises(RuntimeError,match='interrupted'):
        storage.activate(engine,clock_values((T,T+timedelta(seconds=1),None)))
    fixed = storage.activate(engine,lambda:T+timedelta(seconds=50))
    assert fixed.confirmed_at == T+timedelta(seconds=50)
    assert storage.active(engine,T+timedelta(seconds=49)) is None
    with pytest.raises(RuntimeError,match='interrupted'):
        put(engine,'candidate',clock=clock_values((T+timedelta(seconds=1),None)))
    assert put(engine,'candidate',clock=lambda:T+timedelta(seconds=50)) is None
    assert storage.get(engine,'candidate',T+timedelta(seconds=50)) is None


def test_second_stage_binds_first_checksum_and_timestamp(engine):
    value = put(engine)
    changed = T+timedelta(microseconds=500000)
    first_checksum = digest(('receipt','recorded',value.key,value.checksum,changed.isoformat()))
    with engine.begin() as c:
        c.execute(update(storage.ShadowReceipt).where(storage.ShadowReceipt.stage=='recorded')
                  .values(at=changed,checksum=first_checksum))
    assert storage.get(engine,value.key,T+timedelta(seconds=2)) is None


def test_coherently_rehashed_second_stage_before_first_is_invalid(engine):
    value = put(engine)
    changed = T+timedelta(microseconds=500000)
    with session_factory(engine)() as s:
        first = s.scalar(select(storage.ShadowReceipt).where(storage.ShadowReceipt.stage=='recorded'))
    first_at = first.at.replace(tzinfo=UTC)
    checksum = digest(('receipt','confirmed',value.key,value.checksum,first.checksum,
                       first_at.isoformat(),changed.isoformat()))
    with engine.begin() as c:
        c.execute(update(storage.ShadowReceipt).where(storage.ShadowReceipt.stage=='confirmed')
                  .values(at=changed,checksum=checksum))
    assert storage.get(engine,value.key,T+timedelta(seconds=2)) is None


def test_future_ownership_count_requires_day_and_cannot_hide_on_clock_rollback(engine):
    put(engine,'future-claim',kind='attempt',at=T+timedelta(seconds=10),
        clock=lambda:T+timedelta(seconds=11))
    assert storage.count(engine,'attempt',T,day=DAY) == 0
    assert storage.count(engine,'attempt',T,day=DAY,include_future=True) == 1
    assert storage.count(engine,'attempt',T,day=DAY-timedelta(days=1),include_future=True) == 0
    assert storage.list_events(engine,'attempt',T,day=DAY,require_receipt=False) == ()
    with pytest.raises(ValueError,match='future_scope_required'):
        storage.count(engine,'attempt',T,include_future=True)
