from collections import Counter
from datetime import UTC,datetime

from app.curb.bookings import revisions
from app.curb.timeaxis import LOCAL, M, UTC, span
from app.dispatch.store import ReleaseStore, merge_runs, require_window
from app.problems import DocketError
from app.setup import BASE_CENTS, DEFAULT_GRACE_MINUTES, FREE_MINUTES, TIER_LIMIT, UPGRADE_CENTS
from app.source.plates import token


class Dispatcher:
    """宽限放行与杆侧补报的协调器。

    发布只做分钟级切分（可放行 / 留给下笔预约）；补报以事件编号幂等，
    证实有车时仅撤回相撞分钟，凭据本体保留；核价区分准时首免与临停档。
    """

    def __init__(self,store:ReleaseStore,secret:bytes,survey):
        self.store=store
        self.secret=secret
        self.survey=survey

    # ---- 放行发布 -------------------------------------------------------
    def publish(self,payload,bookings):
        berth,raw_start,raw_finish=require_window(payload,self.survey)
        start,finish=raw_start.astimezone(UTC),raw_finish.astimezone(UTC)
        try:
            grace=int(payload.get('grace_minutes',DEFAULT_GRACE_MINUTES))
        except (TypeError,ValueError):
            raise DocketError('REQUEST_BAD','宽限分钟无效') from None
        if not 0<=grace<=1440:
            raise DocketError('REQUEST_BAD','宽限分钟超出范围')
        source=(payload.get('source_reservation') or '').strip() or None
        source_vehicle=None
        if source:
            source_vehicle=next((b['vehicle'] for b in bookings
                                 if b['reservation']==source and b['berth']==berth),None)
        blockers=[b for b in bookings
                  if b['berth']==berth and b['state']=='ACTIVE' and b['reservation']!=source
                  and b['start']<finish and b['end']>start]
        blocked=set()
        for booking in blockers:
            b_start=booking['start'].astimezone(UTC)
            b_end=booking['end'].astimezone(UTC)
            blocked.update(span(max(start,b_start),min(finish,b_end)))
        minutes=[(when,'BLOCKED' if when in blocked else 'PUBLISHED') for when in span(start,finish)]
        credential=ReleaseStore.credential_key(berth,start,finish)
        _,reused=self.store.insert_release(
            credential,{'berth':berth,'start':start,'finish':finish,'grace_minutes':grace,
                        'source_reservation':source,'source_vehicle':source_vehicle},minutes)
        record=self.store.get_release(credential)
        return self._credential_view(record,bookings),reused

    # ---- 杆侧补报 -------------------------------------------------------
    def backfill(self,payload,bookings):
        event_id=str(payload.get('event_id','')).strip()
        if not event_id:
            raise DocketError('BACKFILL_BAD','补报事件编号缺失')
        cached=self.store.backfill_exists(event_id)
        if cached is not None:
            return cached,True
        with self.store.write_lock():
            cached=self.store.backfill_exists(event_id)
            if cached is not None:
                return cached,True
            return self._record_backfill(event_id,payload,bookings)

    def _record_backfill(self,event_id,payload,bookings):
        berth,raw_start,raw_finish=require_window(payload,self.survey,want=('berth','start','end','plate'))
        start,finish=raw_start.astimezone(UTC),raw_finish.astimezone(UTC)
        try:
            vehicle=token(payload['plate'],self.secret)
        except ValueError:
            raise DocketError('BACKFILL_BAD','车牌格式错误') from None
        try:
            grace=int(payload.get('grace_minutes',DEFAULT_GRACE_MINUTES))
        except (TypeError,ValueError):
            raise DocketError('BACKFILL_BAD','宽限分钟无效') from None
        if not 0<=grace<=1440:
            raise DocketError('BACKFILL_BAD','宽限分钟超出范围')
        reservation=(payload.get('reservation') or '').strip() or None
        submitted=datetime.now(UTC).isoformat(timespec='seconds')

        booking=self._match_booking(berth,vehicle,start,reservation,bookings)
        honored=bool(booking) and start<=booking['start']+grace*M
        outcome='HONORED' if honored else 'TEMPORARY'

        # 已发时段若证实有车，只撤掉相撞的可放行分钟，凭据其余部分保留。
        revoked=[]
        for group in self.store.active_minutes(berth,start,finish):
            changed=self.store.revoke_minutes(group['credential'],group['at'],event_id)
            if changed:
                revoked.append({'credential':group['credential'],'minutes':changed,
                                'window':[{'start':a.isoformat(),'end':b.isoformat()}
                                          for a,b in merge_runs(group['at'])]})
        segments,due,paid_minutes=self._price(vehicle,start,finish,honored,booking)
        day=start.astimezone(LOCAL).date().isoformat()
        detail={'event_id':event_id,'berth':berth,'vehicle':vehicle,
                'start':start.isoformat(),'finish':finish.isoformat(),
                'reservation':booking['reservation'] if booking else reservation,
                'outcome':outcome,'grace_minutes':grace,
                'segments':segments,'due_cents':due,'paid_minutes':paid_minutes,
                'revoked':revoked,'submitted_utc':submitted}
        base={'berth':berth,'vehicle':vehicle,'start':start,'finish':finish,
              'submitted_utc':submitted,'reservation':detail['reservation'],'outcome':outcome,
              'day':day,'paid_minutes':paid_minutes,'due_cents':due}
        return self.store.insert_backfill(event_id,base,detail),False

    # ---- 内部规则 -------------------------------------------------------
    def _match_booking(self,berth,vehicle,arrival,reservation,bookings):
        candidates=[b for b in bookings
                    if b['berth']==berth and b['vehicle']==vehicle and b['state']=='ACTIVE'
                    and b['start']<=arrival<b['end']]
        if reservation:
            precise=[b for b in candidates if b['reservation']==reservation]
            if precise:
                return precise[0]
        if not candidates:
            return None
        candidates.sort(key=lambda b:(b['start'],b['reservation']))
        return candidates[0]

    def _price(self,vehicle,start,finish,honored,booking):
        # 同车同日的已入账补报付费分钟沿用档位，午夜按 UTC+08:00 自然日重置。
        prior=Counter()
        for row in self.store.list_backfills():
            if row['vehicle']!=vehicle:
                continue
            for segment in row['detail']['segments']:
                if segment['code'] in {'BASE','UPGRADE'}:
                    prior[segment['day']]+=segment['minutes']
        used=Counter({(vehicle,day):minutes for day,minutes in prior.items()})
        free_left=FREE_MINUTES if honored else 0
        slices=[]
        for when in span(start,finish):
            today=when.astimezone(LOCAL).date().isoformat()
            within=bool(booking) and booking['start']<=when<booking['end']
            if free_left and within:
                code,cents='FREE',0
                free_left-=1
            else:
                code='BASE' if used[vehicle,today]<TIER_LIMIT else 'UPGRADE'
                cents=BASE_CENTS if code=='BASE' else UPGRADE_CENTS
                used[vehicle,today]+=1
            if not slices or (slices[-1]['day'],slices[-1]['code'])!=(today,code):
                slices.append({'start':when.isoformat(),'end':(when+M).isoformat(),'day':today,
                               'code':code,'minutes':0,'rate_cents':cents,'cents':0})
            segment=slices[-1]
            segment['end']=(when+M).isoformat()
            segment['minutes']+=1
            segment['cents']+=cents
        due=sum(s['cents'] for s in slices)
        paid=sum(s['minutes'] for s in slices if s['code'] in {'BASE','UPGRADE'})
        return slices,due,paid

    def _credential_view(self,record,bookings):
        tally=Counter(m['state'] for m in record['minutes'])
        return {'credential':record['credential'],'berth':record['berth'],
                'start':record['start'].isoformat(),'finish':record['finish'].isoformat(),
                'grace_minutes':record['grace_minutes'],
                'source_reservation':record['source_reservation'],
                'published_minutes':tally['PUBLISHED'],'blocked_minutes':tally['BLOCKED'],
                'revoked_minutes':tally['REVOKED'],
                'published_window':[{'start':a.isoformat(),'end':b.isoformat()}
                                    for a,b in merge_runs(m['at'] for m in record['minutes']
                                                          if m['state']=='PUBLISHED')],
                'blocked_window':[{'start':a.isoformat(),'end':b.isoformat()}
                                  for a,b in merge_runs(m['at'] for m in record['minutes']
                                                        if m['state']=='BLOCKED')],
                'revoked_window':[{'start':a.isoformat(),'end':b.isoformat()}
                                  for a,b in merge_runs(m['at'] for m in record['minutes']
                                                        if m['state']=='REVOKED')],
                'minutes':[{'at':m['at'].isoformat(),'state':m['state'],
                            'backfill_id':m['backfill_id']} for m in record['minutes']],
                'created_utc':record['created_utc']}

    def roster(self):
        """当前放行清单：每条凭据的分钟台账与补报痕迹一并可查。"""
        return [self._credential_view(r,[]) for r in self.store.list_releases()]


def active_bookings(clearing):
    """从清算批次的预约版本里提取当前 ACTIVE 预约窗，供放行拦截使用。"""
    histories,_=revisions(clearing.anonymous['bookings'][0],clearing.survey)
    out,seen=[],set()
    for versions in histories.values():
        for row in versions:
            if row['state']!='ACTIVE':
                continue
            key=(row['reservation'],row['berth'],row['vehicle'],row['start'],row['end'])
            if key in seen:
                continue
            seen.add(key)
            out.append({'reservation':row['reservation'],'berth':row['berth'],'vehicle':row['vehicle'],
                        'start':row['start'],'end':row['end'],'effective':row['effective'],
                        'state':'ACTIVE'})
    return sorted(out,key=lambda b:(b['start'],b['reservation']))
