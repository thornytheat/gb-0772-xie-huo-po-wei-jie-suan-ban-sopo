from fastapi import APIRouter, Request

from app.clearing import Clearing
from app.dispatch.service import Dispatcher, active_bookings
from app.problems import DocketError
from app.source.packet import bundle, decode, load

router=APIRouter(prefix='/api/dispatch')


def _context(request:Request,payload:dict|None=None):
    """根据 sample 或 bundle 构造调度器与有效预约；默认取脏样例。"""
    raw=payload if payload is not None else {}
    if 'bundle' in raw:
        files,_=decode({'bundle':raw['bundle']})
    elif raw.get('sample','dirty') not in {'dirty','clean'}:
        raise DocketError('SAMPLE_BAD','找不到选定的清算样例',404)
    else:
        files=bundle(raw.get('sample','dirty'))
    survey,_=load(files)
    clearing=Clearing(files,request.app.state.docket.secret)
    dispatcher=Dispatcher(request.app.state.releases,request.app.state.docket.secret,survey)
    return dispatcher,active_bookings(clearing)


@router.post('/releases')
def publish_release(request:Request,payload:dict):
    dispatcher,bookings=_context(request,payload)
    view,reused=dispatcher.publish(payload,bookings)
    return {'reused':reused,'release':view}


@router.get('/releases')
def roster(request:Request):
    return {'releases':request.app.state.releases.list_releases()}


@router.get('/releases/{credential}')
def read_release(request:Request,credential:str):
    record=request.app.state.releases.get_release(credential)
    if record is None:
        raise DocketError('RELEASE_MISSING','放行凭据不存在',404)
    return record


@router.post('/backfills')
def submit_backfill(request:Request,payload:dict):
    dispatcher,bookings=_context(request,payload)
    detail,reused=dispatcher.backfill(payload,bookings)
    return {'reused':reused,'backfill':detail}


@router.get('/backfills')
def backfills(request:Request):
    return {'backfills':request.app.state.releases.list_backfills()}
