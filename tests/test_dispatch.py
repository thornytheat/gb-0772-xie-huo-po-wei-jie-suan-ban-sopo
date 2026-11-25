from concurrent.futures import ThreadPoolExecutor

from app.dispatch.service import Dispatcher, active_bookings
from app.dispatch.store import ReleaseStore
from app.clearing import Clearing
from app.source.packet import bundle
from app.source.plates import token

SAMPLE='dirty'
SECRET=b'fixture-2026'


def _context(tmp_path):
    clearing=Clearing(bundle(SAMPLE),SECRET)
    store=ReleaseStore(tmp_path/'releases.sqlite')
    dispatcher=Dispatcher(store,SECRET,clearing.survey)
    return clearing,store,dispatcher,active_bookings(clearing)


def test_publish_splits_overlap_minutes_for_next_booking(tmp_path):
    _,_,dispatcher,bookings=_context(tmp_path)
    # BK-09 在 B-01 有效窗 00:50–01:10；放行窗 00:40–01:00，重叠 00:50–01:00 留给预约。
    view,reused=dispatcher.publish(
        {'berth':'B-01','start':'2026-12-01T00:40+08:00','end':'2026-12-01T01:00+08:00'},bookings)
    assert reused is False
    assert (view['published_minutes'],view['blocked_minutes'])==(10,10)
    assert view['blocked_window']==[{'start':'2026-11-30T16:50:00+00:00','end':'2026-11-30T17:00:00+00:00'}]
    assert {m['state'] for m in view['minutes']}=={'PUBLISHED','BLOCKED'}


def test_source_reservation_does_not_block_its_own_released_window(tmp_path):
    _,_,dispatcher,bookings=_context(tmp_path)
    view,_=dispatcher.publish(
        {'berth':'B-01','start':'2026-11-30T23:40+08:00','end':'2026-12-01T00:12+08:00',
         'source_reservation':'BK-07'},bookings)
    assert view['blocked_minutes']==0 and view['published_minutes']==32
    assert view['source_reservation']=='BK-07'


def test_duplicate_publish_emits_single_credential(tmp_path):
    _,store,dispatcher,bookings=_context(tmp_path)
    payload={'berth':'B-02','start':'2026-11-30T23:46+08:00','end':'2026-12-01T00:10+08:00'}
    first,reused_a=dispatcher.publish(payload,bookings)
    second,reused_b=dispatcher.publish(payload,bookings)
    assert (first['credential'],reused_a)==(second['credential'],False)
    assert reused_b is True
    assert len(store.list_releases())==1


def test_two_shifts_submitting_together_get_one_credential(tmp_path):
    _,store,dispatcher,bookings=_context(tmp_path)
    payload={'berth':'B-02','start':'2026-11-30T23:46+08:00','end':'2026-12-01T00:10+08:00'}
    with ThreadPoolExecutor(2) as workers:
        results=list(workers.map(lambda _:dispatcher.publish(payload,bookings),range(2)))
    assert results[0][0]['credential']==results[1][0]['credential']
    assert sorted(r[1] for r in results)==[False,True]
    assert len(store.list_releases())==1


def test_backfill_on_time_keeps_first_free_entitlement(tmp_path):
    _,store,dispatcher,bookings=_context(tmp_path)
    dispatcher.publish({'berth':'B-01','start':'2026-11-30T23:40+08:00',
                        'end':'2026-12-01T00:30+08:00','source_reservation':'BK-07'},bookings)
    # 补报证明原车在预约起点 23:40 准时到场（宽限 10 分钟内），享首免 10 分钟。
    detail,reused=dispatcher.backfill(
        {'event_id':'E-ON-TIME','berth':'B-01','plate':'粤B01234',
         'start':'2026-11-30T23:40+08:00','end':'2026-11-30T23:55+08:00',
         'reservation':'BK-07'},bookings)
    assert reused is False and detail['outcome']=='HONORED'
    assert [(s['code'],s['minutes']) for s in detail['segments']]==[('FREE',10),('BASE',5)]
    assert detail['due_cents']==100 and detail['paid_minutes']==5


def test_backfill_late_inside_release_priced_as_temporary(tmp_path):
    _,_,dispatcher,bookings=_context(tmp_path)
    dispatcher.publish({'berth':'B-01','start':'2026-11-30T23:40+08:00',
                        'end':'2026-12-01T00:30+08:00','source_reservation':'BK-07'},bookings)
    # 实际 23:55 才到，已超 23:40+10 分钟宽限；落在已放行时段，无首免。
    detail,_=dispatcher.backfill(
        {'event_id':'E-LATE','berth':'B-01','plate':'粤B01234',
         'start':'2026-11-30T23:55+08:00','end':'2026-12-01T00:05+08:00'},bookings)
    assert detail['outcome']=='TEMPORARY'
    assert all(s['code']!='FREE' for s in detail['segments'])
    assert detail['paid_minutes']==10 and detail['due_cents']==200


def test_backfill_revokes_only_colliding_minutes_and_keeps_credential(tmp_path):
    _,store,dispatcher,bookings=_context(tmp_path)
    view,_=dispatcher.publish(
        {'berth':'B-01','start':'2026-11-30T23:40+08:00','end':'2026-12-01T00:30+08:00',
         'source_reservation':'BK-07'},bookings)
    detail,_=dispatcher.backfill(
        {'event_id':'E-REVOKE','berth':'B-01','plate':'粤B01234',
         'start':'2026-11-30T23:55+08:00','end':'2026-12-01T00:05+08:00'},bookings)
    assert [r['minutes'] for r in detail['revoked']]==[10]
    record=store.get_release(view['credential'])
    tally={s:sum(1 for m in record['minutes'] if m['state']==s)
           for s in ('PUBLISHED','BLOCKED','REVOKED')}
    # 50 分钟窗口：撤回 10 分钟，其余 40 分钟凭据仍在，且撤回分钟带补报编号可查。
    assert tally=={'PUBLISHED':40,'BLOCKED':0,'REVOKED':10}
    assert all(m['backfill_id']=='E-REVOKE' for m in record['minutes'] if m['state']=='REVOKED')
    assert store.list_releases()[0]['credential']==view['credential']


def test_rerun_same_backfill_never_overwrites_result(tmp_path):
    _,store,dispatcher,bookings=_context(tmp_path)
    dispatcher.publish({'berth':'B-01','start':'2026-11-30T23:40+08:00',
                        'end':'2026-12-01T00:30+08:00','source_reservation':'BK-07'},bookings)
    payload={'event_id':'E-RERUN','berth':'B-01','plate':'粤B01234',
             'start':'2026-11-30T23:55+08:00','end':'2026-12-01T00:05+08:00'}
    first,reused_a=dispatcher.backfill(payload,bookings)
    # 旧清单重跑，哪怕改了内容，也只返回首次结果，撤回不重复计数。
    tampered=payload|{'end':'2026-12-01T00:20+08:00','plate':'粤B11111'}
    second,reused_b=dispatcher.backfill(tampered,bookings)
    assert reused_a is False and reused_b is True and first==second
    stored=store.list_backfills()
    assert len(stored)==1 and stored[0]['event_id']=='E-RERUN'
    release=store.list_releases()[0]
    assert sum(1 for m in release['minutes'] if m['state']=='REVOKED')==10


def test_backfill_idempotent_under_parallel_rerun(tmp_path):
    _,store,dispatcher,bookings=_context(tmp_path)
    dispatcher.publish({'berth':'B-01','start':'2026-11-30T23:40+08:00',
                        'end':'2026-12-01T00:30+08:00','source_reservation':'BK-07'},bookings)
    payload={'event_id':'E-PARALLEL','berth':'B-01','plate':'粤B01234',
             'start':'2026-11-30T23:55+08:00','end':'2026-12-01T00:05+08:00'}
    with ThreadPoolExecutor(2) as workers:
        results=list(workers.map(lambda _:dispatcher.backfill(payload,bookings),range(2)))
    assert results[0][0]==results[1][0] and sorted(r[1] for r in results)==[False,True]
    release=store.list_releases()[0]
    assert sum(1 for m in release['minutes'] if m['state']=='REVOKED')==10


def test_revoked_minutes_are_not_revoked_twice_by_another_event(tmp_path):
    _,store,dispatcher,bookings=_context(tmp_path)
    dispatcher.publish({'berth':'B-01','start':'2026-11-30T23:40+08:00',
                        'end':'2026-12-01T00:30+08:00','source_reservation':'BK-07'},bookings)
    window={'berth':'B-01','start':'2026-11-30T23:55+08:00','end':'2026-12-01T00:05+08:00'}
    first,_=dispatcher.backfill({'event_id':'E-A','plate':'粤B01234',**window},bookings)
    second,_=dispatcher.backfill({'event_id':'E-B','plate':'粤B22222',**window},bookings)
    assert sum(r['minutes'] for r in first['revoked'])==10
    assert second['revoked']==[]
    release=store.list_releases()[0]
    assert sum(1 for m in release['minutes'] if m['state']=='REVOKED')==10
    assert {m['backfill_id'] for m in release['minutes'] if m['state']=='REVOKED'}=={'E-A'}


def test_temporary_tier_carries_across_backfills_same_day(tmp_path):
    _,_,dispatcher,bookings=_context(tmp_path)
    # 第一笔临停 20 分钟跨午夜：11/30 5 分钟基础档，12/01 用满 15 分钟基础档。
    first,_=dispatcher.backfill(
        {'event_id':'E-T1','berth':'B-01','plate':'粤B01234',
         'start':'2026-11-30T23:55+08:00','end':'2026-12-01T00:15+08:00'},bookings)
    assert first['outcome']=='TEMPORARY' and first['paid_minutes']==20
    assert first['due_cents']==400
    # 同日（UTC+08:00 仍为 12/01 凌晨）第二笔直接进升级价 40 分/分。
    second,_=dispatcher.backfill(
        {'event_id':'E-T2','berth':'B-01','plate':'粤B01234',
         'start':'2026-12-01T00:20+08:00','end':'2026-12-01T00:25+08:00'},bookings)
    assert second['outcome']=='TEMPORARY'
    assert [(s['day'],s['code'],s['minutes'],s['cents']) for s in second['segments']]==[
        ('2026-12-01','UPGRADE',5,200)]


def test_publish_rejects_window_outside_survey(tmp_path):
    from app.problems import DocketError
    _,_,dispatcher,bookings=_context(tmp_path)
    try:
        dispatcher.publish({'berth':'B-01','start':'2026-12-01T02:00+08:00',
                            'end':'2026-12-01T02:10+08:00'},bookings)
    except DocketError as error:
        assert error.code=='RANGE'
    else:  # pragma: no cover
        raise AssertionError('窗外放行应被拒绝')


def test_backfill_requires_event_and_plate(tmp_path):
    from app.problems import DocketError
    _,_,dispatcher,bookings=_context(tmp_path)
    for payload,code in [
        ({'berth':'B-01','start':'2026-11-30T23:55+08:00','end':'2026-12-01T00:05+08:00',
          'plate':'粤B01234'},'BACKFILL_BAD'),
        ({'event_id':'E-X','berth':'B-01','start':'2026-11-30T23:55+08:00',
          'end':'2026-12-01T00:05+08:00','plate':'-'},'BACKFILL_BAD')]:
        try:
            dispatcher.backfill(payload,bookings)
        except DocketError as error:
            assert error.code==code
        else:  # pragma: no cover
            raise AssertionError('非法补报应被拒绝')


def test_roster_and_backfills_are_queryable(tmp_path):
    _,store,dispatcher,bookings=_context(tmp_path)
    view,_=dispatcher.publish(
        {'berth':'B-01','start':'2026-11-30T23:40+08:00','end':'2026-12-01T00:10+08:00',
         'source_reservation':'BK-07'},bookings)
    dispatcher.backfill({'event_id':'E-Q','berth':'B-01','plate':'粤B01234',
                         'start':'2026-11-30T23:55+08:00','end':'2026-12-01T00:05+08:00'},bookings)
    roster=dispatcher.roster()
    assert [r['credential'] for r in roster]==[view['credential']]
    assert roster[0]['revoked_minutes']==10
    events=store.list_backfills()
    assert events[0]['detail']['event_id']=='E-Q'
    assert events[0]['vehicle']==token('粤B01234',SECRET)
