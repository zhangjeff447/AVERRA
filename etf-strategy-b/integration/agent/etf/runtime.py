"""One bounded daily plan; restart recovery and fresh reconciliation precede every order."""
import hashlib,json,time
from datetime import datetime,timezone
from decimal import Decimal
from .cycle import POLICY,plan,money,slow_average,defense_reason
from .funds import PortfolioValuationMismatch, normalize
from .calendar import ET,previous_session
from .reconciliation import validate_history,validate_activation_positions
from .recovery import require_window

# Robinhood's total_value can lag our own fills by a few seconds. After THIS run
# has filled, re-read on this schedule before giving up; no new order is placed
# while the valuation is unverified.
POST_FILL_RETRY_SECONDS=(3,5,10)

def day_fills(journal,day):
 """Summarize ledger fills by their ET execution date, including late approvals."""
 from datetime import timedelta
 start=datetime.fromisoformat(day).replace(tzinfo=ET)
 end=start+timedelta(days=1)
 rows={}
 fills=journal.db.execute(
  'SELECT i.id,i.symbol,i.side,i.status,f.gross,f.quantity,f.executed_at '
  'FROM etf_fills f JOIN etf_intents i ON i.id=f.intent_id ORDER BY f.executed_at,f.id')
 for fill in fills:
  if not start<=datetime.fromisoformat(fill['executed_at'])<end:continue
  row=rows.setdefault(fill['id'],dict(intent_id=fill['id'],symbol=fill['symbol'],side=fill['side'],
                       status=fill['status'],gross=Decimal(0),quantity=Decimal(0)))
  row['gross']+=money(fill['gross'])
  row['quantity']+=money(fill['quantity'])
 return [dict(row,gross=str(row['gross'].quantize(Decimal('.01'))),quantity=str(row['quantity']))
         for row in rows.values()]

def post_fill_result(journal,mode_context,results,difference):
 for item in results:
  execution=item['execution']
  if iid:=execution.get('intent_id'):
   execution['status']=journal.get(iid)['status']
 return dict(mode_context,selected=None,reason='FILLED_VALUATION_PENDING',executions=results,
             cash_basis='POST_FILL_UNVERIFIED',valuation_difference_usd=str(difference))

def checked(journal,raw,*,now):
 journal.reconcile(raw['orders'],complete=raw['complete_orders'])
 validate_history(journal,raw);validate_activation_positions(journal,raw)
 journal.sync_rotation();day=now.astimezone(ET).date().isoformat()
 if 'external_cash_events' in raw['funds']:journal.observe_external_cash_events(raw['funds']['external_cash_events'])
 try:
  snapshot=normalize(raw,now=now,held_cash=journal.unsettled_sale_cash(day))
 except PortfolioValuationMismatch as exc:
  journal.audit('PORTFOLIO_VALUATION_MISMATCH',dict(difference_usd=str(exc.difference)))
  raise
 journal.bind_account(snapshot['account_hash'])
 return snapshot

def cycle(journal,broker,signal,*,live=False,now=None,max_orders=6,window=None,risk_only=False,breadth=None):
 now=now or datetime.now(timezone.utc);day=now.astimezone(ET).date().isoformat()
 if signal['date']!=previous_session(day):raise ValueError('signal_session_stale')
 key_suffix=POLICY['version']+':'+day
 base={k:v for k,v in signal.items() if k not in ('sector_regime','sector_history_sha256')}
 checks={'rf_signal:'+key_suffix:base}
 mode_context={}
 if POLICY.get('strategy_b_core_cash'):
  from .strategy_b_cash import context
  mode_context=context(signal)
 elif POLICY.get('sector_defense'):
  defensive=defense_reason(signal,use_sector=True)
  mode_context=dict(phase='DEFENSIVE' if defensive else 'ATTACK',signal_date=signal['date'],
                    sector_regime=signal['sector_regime'],defense_reason=defensive)
  checks['rf_sector_signal:'+key_suffix]=signal
 hashes={key:hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest() for key,value in checks.items()}
 for key,fingerprint in hashes.items():
  old=journal.load(key)
  if old and old!=fingerprint:raise ValueError('completed_daily_signal_changed')
 if live:
  with journal.transaction():
   for key,fingerprint in hashes.items():journal.save(key,fingerprint)
 results=[]
 # A submitted-but-unconfirmed order blocks further orders until broker history
 # confirms it. The loop can advance multiple legs only after dated fills.
 for _ in range(max_orders):
  raw=broker.read()
  try:
   try:
    snapshot=checked(journal,raw,now=datetime.now(timezone.utc) if live else now)
   except PortfolioValuationMismatch as mismatch:
    if (live and window and not risk_only and not journal.active() and not journal.legacy_active()
        and any(journal.filled_gross(item['execution']['intent_id'])>0 for item in results
                if item['execution'].get('intent_id'))):
     # Our own fills were just reconciled into the ledger; the broker total
     # may simply not include them yet. Retry, and if it still disagrees,
     # stop ordering but keep the executions instead of failing the run.
     for delay in POST_FILL_RETRY_SECONDS:
      time.sleep(delay)
      try:require_window(window)
      except ValueError:return post_fill_result(journal,mode_context,results,mismatch.difference)
      raw=broker.read()
      try:
       snapshot=checked(journal,raw,now=datetime.now(timezone.utc))
       try:require_window(window)
       except ValueError:return post_fill_result(journal,mode_context,results,mismatch.difference)
       break
      except PortfolioValuationMismatch as again:
       mismatch=again
     else:
      return post_fill_result(journal,mode_context,results,mismatch.difference)
    elif not live or not window or risk_only or results or journal.active() or journal.legacy_active():
     raise
    else:
     # Refresh both sides of the comparison before any order is prepared.
     require_window(window)
     raw=broker.read()
     snapshot=checked(journal,raw,now=datetime.now(timezone.utc))
     require_window(window)
  except ValueError as exc:
   if str(exc)!='pending_external_credit_wait':raise
   if POLICY.get('strategy_b_core_cash'):
    return dict(mode_context,selected=None,reason='WAIT_PENDING_DEPOSIT',executions=results,
                cash_basis='UNVERIFIED_PENDING_DEPOSIT',reported_cash=raw['funds'].get('reported_cash'),
                pending_external_credits=raw['funds']['pending_external_credits'])
   period=POLICY.get('slow_ma_period',100)
   defensive=defense_reason(signal,use_sector=bool(POLICY.get('sector_defense')))
   return dict(version=POLICY['version'],phase='DEFENSIVE' if defensive else 'ATTACK',
    **({'sector_regime':signal['sector_regime'],'defense_reason':defensive} if POLICY.get('sector_defense') else {}),
    signal_date=signal['date'],sma50=signal['sma50'],slow_ma_period=period,**{'sma'+str(period):str(slow_average(signal))},
    selected=None,reason='WAIT_PENDING_DEPOSIT',executions=results,cash_basis='UNVERIFIED_PENDING_DEPOSIT',
    reported_cash=raw['funds'].get('reported_cash'),pending_external_credits=raw['funds']['pending_external_credits'])
  if live:journal.recover_unsubmitted()
  if journal.halt_reason():return dict(mode_context,reason='TRADING_HALTED:'+journal.halt_reason(),selected=None,executions=results)
  if journal.active() or journal.legacy_active():return dict(mode_context,reason='BLOCKED_BY_UNRESOLVED_INTENT',selected=None,executions=results)
  if raw.get('pending_approvals'):return dict(mode_context,reason='WAIT_PENDING_APPROVAL',selected=None,executions=results)
  minute_context={}
  if POLICY.get('intraday_enabled') and not risk_only:
   from .stop_loss import protection
   if not protection(snapshot,signal,journal)['triggered']:
    try:minute_context=broker.minute_data(datetime.now(timezone.utc) if live else now)
    except Exception as exc:minute_context=dict(unavailable=type(exc).__name__)
  snapshot['intraday']=minute_context
  result=plan(snapshot,signal,journal,now=now,risk_only=risk_only,persist_allocation=live,evaluation_time=datetime.now(timezone.utc),breadth=breadth)
  if not live or not result['selected']:
   if not result['selected'] and any(x['plan']['reason']=='STOP_LOSS_6_6_ALL_TQQQ' for x in results):result['reason']='STOP_LOSS_FILLED_WAIT_SETTLEMENT'
   result['cash_basis']='VERIFIED_CURRENT';result['executions']=results;return result
  if result['selected']['reason']=='STOP_LOSS_6_6_ALL_TQQQ' and not journal.load('tqqq_stop_latch'):
   with journal.transaction():
    evidence=dict(signal_date=signal['date'],triggered_at=now.isoformat(),scope='ALL_ACCOUNT_SHARES',average_cost=snapshot['tqqq_average_cost'])
    journal.save('tqqq_stop_latch',evidence);journal.audit('TQQQ_STOP_TRIGGERED',evidence)
  elif result['selected']['symbol']=='TQQQ' and result['selected']['side']=='BUY' and journal.load('tqqq_stop_latch'):
   with journal.transaction():
    journal.audit('TQQQ_STOP_RECOVERED',journal.load('tqqq_stop_latch'));journal.db.execute("DELETE FROM etf_meta WHERE key='tqqq_stop_latch'")
  from .execution import execute
  def replan(fresh,exclude_intent=None):
   fresh['intraday']=minute_context
   return plan(fresh,signal,journal,now=now,exclude_intent=exclude_intent,risk_only=risk_only,evaluation_time=datetime.now(timezone.utc),breadth=breadth)
  execution=execute(journal,broker,result['selected']['decision_id'],result,replan=replan,live=True,window=window)
  actual_plan=json.loads(journal.get(execution['intent_id'])['plan']) if execution.get('intent_id') else result['selected']
  results.append(dict(plan=actual_plan,execution=execution))
  # Always re-read next iteration. Never assume an accepted response is a fill.
  if execution['status'] in ('EXECUTION_UNKNOWN','AWAITING_USER_APPROVAL','WAIT'):
   result['cash_basis']='PRE_ORDER';result['execution']=execution;result['executions']=results;return result
 result['cash_basis']='PRE_ORDER';result['executions']=results;result['reason']='BOUNDED_CYCLE_COMPLETE';return result

def report(result):
 if result.get('sector_regime') or result.get('strategy_b_state'):
  from .telegram_reports import trend as mode_label
  trend=mode_label(result)
 else:
  trend='长期上涨条件成立' if result.get('phase')=='ATTACK' else '长期上涨条件未成立' if result.get('phase')=='DEFENSIVE' else '数据待核实'
 lines=['ETF Agent | '+POLICY['version'],'市场行情：'+trend]
 period=result.get('slow_ma_period',POLICY.get('slow_ma_period',100))
 if 'sma50' in result:lines.append('QQQ 均线：50日 '+format(Decimal(result['sma50']),'.2f')+' / '+str(period)+'日 '+format(Decimal(result['sma'+str(period)]),'.2f')+'（'+result['signal_date']+'收盘）')
 selected=result.get('selected')
 if result.get('executions'):lines.append('信号：'+' / '.join(item['plan']['side']+' '+item['plan']['symbol'] for item in result['executions']))
 else:lines.append('信号：'+(selected['side']+' '+selected['symbol']+' $'+selected['amount'] if selected else 'WAIT'))
 if 'safe_cash' in result:lines.append(('下单前可用自有资金：$' if result.get('cash_basis')=='PRE_ORDER' else '可用自有资金：$')+result['safe_cash'])
 if not result.get('executions') and result.get('day_fills'):
  lines.append('今日已成交：'+' / '.join(f['side']+' '+f['symbol']+' $'+f['gross'] for f in result['day_fills']))
 for item in result.get('executions',[]):
  order=item['plan'];execution=item['execution'];price=order.get('price')
  lines.append(order['symbol']+' '+order['side']+' 计划 $'+order['amount']+' | 参考价 $'+format(Decimal(price),'.4f')+' | '+execution['status'])
 lines.append('原因：'+str(result.get('reason','待核实')))
 return '\n'.join(lines)
