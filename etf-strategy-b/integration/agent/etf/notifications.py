"""Durable notifications; every format is also available without sending."""
from datetime import datetime,timezone
from pathlib import Path
from uuid import uuid4
from urllib.error import HTTPError
import sqlite3
from .calendar import ET,scheduled
from .telegram_reports import decision_notification
ROOT=Path(__file__).resolve().parents[1]


def authorization_required(reason):
    while reason is not None:
        if isinstance(reason,HTTPError) and reason.code==401:
            return True
        text=str(reason).lower()
        if any(code in text for code in ('401: unauthorized','401 unauthorized','auth_required',
                'robinhood_mcp_oauth_missing_or_ambiguous','robinhood_mcp_oauth_token_missing',
                'robinhood_mcp_oauth_refresh_unavailable','robinhood_mcp_oauth_refresh_invalid','invalid_grant')):
            return True
        reason=getattr(reason,'__cause__',None)
    return False


def authorization_notice(store, *, recovered=False):
    # Executor and report worker can see the same incident. Claim it atomically.
    store.db.execute('BEGIN IMMEDIATE')
    try:
        row=store.db.execute("SELECT run_id FROM faults WHERE run_id LIKE 'rf-auth:%' "
                             "AND category='AUTH_REQUIRED' AND resolved_at IS NULL LIMIT 1").fetchone()
        if recovered and row:
            store.enqueue(row[0]+':recovered','BROKER_AUTH_RECOVERED',
                'ETF 连接｜Robinhood 已恢复\n账户和订单已完成只读核对。下一次交易由正常窗口判定。')
            store.resolve(row[0])
        elif not recovered and not row:
            key='rf-auth:'+uuid4().hex
            store.fault(key,'AUTH_REQUIRED')
            store.enqueue(key,'BROKER_AUTH_REQUIRED',
                'ETF 连接｜Robinhood 需要重新授权\n本轮已阻止新提交。请重新连接 Robinhood，恢复后先核对账户与订单。')
        store.db.commit()
    except BaseException:
        store.db.rollback()
        raise

def window_fills(journal,window):
    from datetime import timedelta
    start=datetime.fromisoformat(window).replace(tzinfo=ET)
    end=start+timedelta(minutes=5)
    rows=journal.db.execute(
        'SELECT i.broker_id,i.symbol,i.side,f.id,f.gross,f.executed_at '
        'FROM etf_fills f JOIN etf_intents i ON i.id=f.intent_id ORDER BY f.executed_at,f.id')
    return [dict(key=row['broker_id']+':'+row['id'],symbol=row['symbol'],side=row['side'],
                 gross=row['gross'],executed_at=row['executed_at']) for row in rows
            if row['broker_id'] and start<=datetime.fromisoformat(row['executed_at'])<end]


def enqueue(result,journal):
 from agent_ops import Operations
 now=(datetime.fromisoformat(result['run_at']) if result.get('run_at') else datetime.now(timezone.utc)).astimezone(ET)
 import hashlib,json
 event=dict(day=now.date().isoformat(),window=scheduled(now),version=result.get('version'),phase=result.get('phase'),selected=result.get('selected'),reason=result.get('reason'),executions=result.get('executions',[]))
 key=hashlib.sha256(json.dumps(event,sort_keys=True).encode()).hexdigest()
 valuation=ROOT.parent.parent/'etf-duo-reporter/data/valuation-telegram.json'
 text=decision_notification(result,ROOT.parent/'data',now,valuation_path=valuation)
 with __import__('contextlib').closing(Operations(ROOT.parent/'state/operations.sqlite')) as store:
  if authorization_required(result.get('reason')):
   try:authorization_notice(store)
   except sqlite3.Error:pass
  if window := result.get('deferred_window'):
   return store.enqueue('rf-valuation:'+window,'DECISION_DEFERRED',text)
  if window := result.get('window'):
   return store.enqueue('rf-window:'+window,'DECISION',text)
  return store.enqueue('rf-decision:'+key,'DECISION',text)


def configure_sender(broker):
 from tools.ops_utils import load_env_file
 secrets=load_env_file(Path(broker.adapter.credential_config).parent.parent/'.env.secrets')
 token=secrets.get('TELEGRAM_BOT_TOKEN');chat=secrets.get('TELEGRAM_CHAT_ID')
 if not token or not chat:raise ValueError('existing_etf_sender_missing')
 broker.adapter.telegram_bot_token=token;broker.adapter.telegram_chat_id=chat
