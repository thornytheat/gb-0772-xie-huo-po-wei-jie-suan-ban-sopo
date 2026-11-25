def test_dispatch_board_renders_empty_state(client):
    page=client.get('/dispatch')
    assert page.status_code==200 and '调度放行对账' in page.text
    assert '尚未发布放行时段' in page.text


def test_publish_release_then_roster_visible_on_board(client):
    payload={'sample':'dirty','berth':'B-01',
             'start':'2026-12-01T00:40+08:00','end':'2026-12-01T01:00+08:00'}
    first=client.post('/api/dispatch/releases',json=payload).json()
    second=client.post('/api/dispatch/releases',json=payload).json()
    assert first['release']['credential']==second['release']['credential']
    assert (first['reused'],second['reused'])==(False,True)
    assert (first['release']['published_minutes'],first['release']['blocked_minutes'])==(10,10)

    roster=client.get('/api/dispatch/releases').json()['releases']
    assert len(roster)==1 and roster[0]['credential']==first['release']['credential']
    detail=client.get('/api/dispatch/releases/'+first['release']['credential'])
    assert detail.status_code==200 and len(detail.json()['minutes'])==20

    page=client.get('/dispatch').text
    assert first['release']['credential'][:12] in page and '留给预约' in page


def test_backfill_flow_revokes_minutes_and_prices_temporary(client):
    client.post('/api/dispatch/releases',json={
        'sample':'dirty','berth':'B-01','source_reservation':'BK-07',
        'start':'2026-11-30T23:40+08:00','end':'2026-12-01T00:30+08:00'})
    payload={'sample':'dirty','event_id':'E-API-1','berth':'B-01','plate':'粤B01234',
             'start':'2026-11-30T23:55+08:00','end':'2026-12-01T00:05+08:00'}
    result=client.post('/api/dispatch/backfills',json=payload).json()
    assert result['reused'] is False and result['backfill']['outcome']=='TEMPORARY'
    assert result['backfill']['due_cents']==200
    assert sum(r['minutes'] for r in result['backfill']['revoked'])==10

    replay=client.post('/api/dispatch/backfills',json=payload).json()
    assert replay['reused'] is True and replay['backfill']==result['backfill']

    events=client.get('/api/dispatch/backfills').json()['backfills']
    assert [e['event_id'] for e in events]==['E-API-1']
    page=client.get('/dispatch').text
    assert '临停核价' in page and 'E-API-1' in page


def test_on_time_backfill_keeps_first_free(client):
    client.post('/api/dispatch/releases',json={
        'sample':'dirty','berth':'B-01','source_reservation':'BK-07',
        'start':'2026-11-30T23:40+08:00','end':'2026-12-01T00:30+08:00'})
    result=client.post('/api/dispatch/backfills',json={
        'sample':'dirty','event_id':'E-API-2','berth':'B-01','plate':'粤B01234',
        'reservation':'BK-07',
        'start':'2026-11-30T23:40+08:00','end':'2026-11-30T23:55+08:00'}).json()['backfill']
    assert result['outcome']=='HONORED'
    assert [(s['code'],s['minutes']) for s in result['segments']]==[('FREE',10),('BASE',5)]
    assert result['due_cents']==100


def test_dispatch_validation_errors(client):
    bad=client.post('/api/dispatch/releases',json={
        'sample':'dirty','berth':'B-01',
        'start':'2026-12-01T02:00+08:00','end':'2026-12-01T02:10+08:00'})
    assert (bad.status_code,bad.json()['code'])==(422,'RANGE')
    missing=client.post('/api/dispatch/backfills',json={'sample':'dirty'})
    assert (missing.status_code,missing.json()['code'])==(422,'BACKFILL_BAD')
    missing_release=client.get('/api/dispatch/releases/nope')
    assert (missing_release.status_code,missing_release.json()['code'])==(404,'RELEASE_MISSING')


def test_invalid_grace_returns_domain_error(client):
    r=client.post('/api/dispatch/releases',json={
        'sample':'dirty','berth':'B-01','grace_minutes':'soon',
        'start':'2026-12-01T00:40+08:00','end':'2026-12-01T01:00+08:00'})
    assert (r.status_code,r.json()['code'])==(422,'REQUEST_BAD')
    r=client.post('/api/dispatch/backfills',json={
        'sample':'dirty','event_id':'E-BAD-GRACE','berth':'B-01','plate':'粤B01234',
        'grace_minutes':-1,'start':'2026-11-30T23:55+08:00','end':'2026-12-01T00:05+08:00'})
    assert (r.status_code,r.json()['code'])==(422,'BACKFILL_BAD')


def test_dispatch_endpoints_accept_full_bundle(client):
    from app.source.packet import bundle
    files=bundle('clean')
    result=client.post('/api/dispatch/releases',json={
        'bundle':files,'berth':'B-02',
        'start':'2026-11-30T23:46+08:00','end':'2026-12-01T00:10+08:00',
        'source_reservation':'BK-08'}).json()
    assert result['reused'] is False and result['release']['blocked_minutes']==0
