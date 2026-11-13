"""Read-only report/delivery worker. GuardedTransport permanently denies orders."""
import fcntl
import json
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
import sqlite3
from agent_ops import Operations,category
from .broker import NativeAdapter,Broker
from .cycle import POLICY,core_symbols,money
from .funds import normalize,timestamp
from .journal import Journal
from .reconciliation import validate_history
from .notifications import authorization_notice, authorization_required, configure_sender
from .calendar import is_session
def is_trading_day(local):return is_session(local.date().isoformat())

ET=ZoneInfo('America/New_York')
ROOT=Path(__file__).resolve().parents[1]


def due(at):
    local=at.astimezone(ET)
    return is_trading_day(local) and (15,31)<=(local.hour,local.minute)<(20,0)


def latest_result(root,day):
    path=root/'logs/cycles.jsonl'
    if not path.exists():return {}
    for line in reversed(path.read_text().splitlines()):
        try:
            item=json.loads(line)
            if datetime.fromisoformat((item.get('run_at') or '').replace('Z','+00:00')).astimezone(ET).date().isoformat()==day:return item
        except (ValueError,TypeError):continue
    return {}


def compact_monitor(observation):
    prices = observation.get('current_drawdown', {})
    fields = []
    for symbol in ('SPY', 'QQQ', 'SMH'):
        row = prices.get(symbol, {})
        value = (row.get('drawdown_pct') or {}).get('252')
        if row.get('status') == 'AVAILABLE' and isinstance(value, (int, float)):
            fields.append(f'{symbol} {abs(value):.1f}%')
    if not fields:
        return '回撤／金融压力｜待核实'
    stress = {'NORMAL':'正常','ELEVATED':'偏高','HIGH':'高','WATCH':'观察中'}.get(
        observation.get('overall_financial_stress'), '待核实')
    return '距252日高点｜'+' · '.join(fields)+'｜金融压力 '+stress


def target_weights_line(result):
    if POLICY.get('strategy_b_core_cash'):
        from .strategy_b_cash import rules
        cfg = rules()
        strategic = cfg['strategic_core_weights']
        state = cfg['states'].get(result.get('strategy_b_state'))
        baseline = '战略基准（核心仓）｜'+ ' · '.join(f'{s} {money(strategic[s]):.0%}' for s in ('VOO','SMH'))
        cash = ('新增资金分配｜'+ ' · '.join(f"{s} {money(state['new_core_cash'][s]):.0%}" for s in ('VOO','SMH'))
                if state else '新增资金分配｜模式待核实')
        risk = ' · '.join(result.get('risk_signals', ())) or ('未触发' if state else '待核实')
        return '\n'.join((baseline,cash,'TQQQ｜目标 0% · 禁止下单','风险信号｜'+risk))
    phase = result.get('phase')
    if POLICY.get('sector_defense') and not result.get('sector_regime'):
        phase = None
    targets = {'ATTACK':POLICY['targets'],
               'DEFENSIVE':POLICY.get('defensive_targets',POLICY['core_targets'])}.get(phase)
    if targets is None:
        return '当前目标｜模式待核实'
    smh,primary=core_symbols()
    return '当前目标｜'+' · '.join(f'{symbol} {money(targets.get(symbol,"0")):.0%}'
                                for symbol in (primary,smh,'TQQQ'))


def holdings_report(journal,raw,result,at):
    """Reconcile only an in-memory copy. Closing/stale quotes are labelled."""
    if raw.get('account_hash')!=journal.load('account_hash'):raise ValueError('report_account_mismatch')
    copy=Journal(':memory:')
    try:
        journal.db.backup(copy.db);copy.reconcile(raw['orders'],complete=raw['complete_orders']);validate_history(copy,raw)
        if copy.halt_reason():raise ValueError('report_reconciliation_blocked')
        day = at.astimezone(ET).date().isoformat()
        from .telegram_reports import usd, trend, market_position, execution_summary, portfolio_symbols
        total = money(raw['reported_net_assets'])
        cash = money(raw['funds']['reported_cash'])
        rebuilt = cash
        positions = []
        delayed = False
        for symbol in portfolio_symbols(raw):
            quote = raw['quotes'][symbol]
            time = timestamp(quote['timestamp'])
            price = money(quote['last'])
            value = money(raw['positions'][symbol]['quantity'])*price
            if time > datetime.now(timezone.utc)+timedelta(seconds=5) or price <= 0:
                raise ValueError('report_quote_invalid')
            rebuilt += value
            delayed |= bool(quote.get('is_stale') or not quote.get('is_realtime'))
            positions.append(f'{symbol} {value/total:.0%}' if total else f'{symbol} 待核实')
        try:
            safe = normalize(raw)['safe_cash']
        except ValueError:
            safe = None
        lines = [f'ETF 日报｜{day}',
                 ('当前模式｜' if result.get('sector_regime') or POLICY.get('strategy_b_core_cash') else '中期趋势｜')+trend(result),
                 market_position(result),
                 '最近执行｜'+(result.get('execution_receipt_label') or execution_summary(result)),
                 f'总资产 {usd(total)}｜现金 {usd(cash)}｜可用 {usd(safe)}',
                 '实际持仓｜'+' · '.join(positions),
                 target_weights_line(result)]
        if delayed or abs(rebuilt-total)>Decimal('.25'):
            lines.append('持仓权重为参考估计')
        if copy.active() or copy.legacy_active():
            lines.append('有订单待处理，请核对 Robinhood')
        if result.get('report_trading_halted') is True or result.get('report_trading_authorized') is False:
            lines.append('交易暂停或未获实盘许可')
        copy.sync_rotation()
        unsettled = copy.unsettled_sale_cash(day)
        if unsettled > 0:
            lines.append('TQQQ 待结算 '+usd(unsettled))
        if POLICY.get('daily_buy_limit') is not None:
            lines.append('买入上限｜每日合计'+usd(POLICY['daily_buy_limit'])+
                         '／每窗口合计'+usd(POLICY['per_window_buy_limit']))
        from .price_levels import build
        levels = build(ROOT.parent/'data',at)
        references = [f"{symbol} {usd(row['support'])}/{usd(row['breakout'])}"
                      for symbol,row in levels.get('symbols',{}).items() if row.get('status')=='OK']
        if references:
            lines.append('支撑/突破｜'+' · '.join(references))
        return '\n'.join(lines)
    finally:copy.db.close()


def send(text,adapter):
    # Return explicit evidence. Transport uncertainty never becomes a retry.
    import requests
    from .telegram_reports import DETAIL_BREAK
    payload={'chat_id':adapter.telegram_chat_id,'text':text}
    summary, divider, details=text.partition(DETAIL_BREAK)
    if text.startswith('ETF 决策') and divider and details:
        payload['entities']=[{'type':'expandable_blockquote',
                              'offset':len((summary+'\n\n').encode('utf-16-le'))//2,
                              'length':len((divider[2:]+details).encode('utf-16-le'))//2}]
    response=requests.post('https://api.telegram.org/bot'+adapter.telegram_bot_token+'/sendMessage',
                           json=payload,timeout=15)
    data=response.json();mid=(data.get('result') or {}).get('message_id')
    if response.status_code==200 and data.get('ok') is True and type(mid) is int and mid>0:return dict(state='SENT',message_id=mid)
    if data.get('ok') is False:return dict(state='REJECTED',error='TELEGRAM_HTTP_'+str(response.status_code))
    return dict(state='UNKNOWN',error='MISSING_CONFIRMED_RECEIPT')


def drain(store,adapter,sender=send):
    # This worker's exclusive file lock proves no previous sender is running.
    store.interrupted_sends()
    pending=store.db.execute("SELECT key,body FROM outbox WHERE state='PENDING' ORDER BY created_at,key").fetchall()
    if not pending:return
    configure_sender(type('Sender',(),{'adapter':adapter})())  # Before claim: missing credentials cannot send.
    for row in pending:
        if not store.claim(row['key']):continue
        try:
            result=sender(row['body'],adapter)
            store.finish(row['key'],result['state'],message_id=result.get('message_id'),error=result.get('error'))
        except Exception:store.finish(row['key'],'UNKNOWN',error='TRANSPORT_UNCERTAIN')


def run(root=ROOT,at=None,*,adapter_factory=NativeAdapter,sender=send):
    at=at or datetime.now(timezone.utc);local=at.astimezone(ET)
    if not is_trading_day(local) or not (9,30)<=(local.hour,local.minute)<(20,0):return 0
    base=root.parent;state=base/'state';state.mkdir(exist_ok=True)
    with (state/'notifications.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return 0
        store=Operations(state/'operations.sqlite')
        try:
            config=json.loads((base/'config/runtime.json').read_text())
            import hashlib
            if (config.get('deployment_authorized') is not True and config.get('notifications_authorized') is not True) or config.get('policy_hash')!=hashlib.sha256((root/'etf/policy.json').read_bytes()).hexdigest():raise ValueError('report_configuration_unverified')
            adapter=adapter_factory(str(state/'report-reader'),config['credential_config'],live=False)
            key='holdings:'+local.date().isoformat()
            if (9,30)<=(local.hour,local.minute)<(20,0):  # Fills do not wait for the daily report.
                failures=[e for e in store.events(key,limit=1) if e['stage']=='READ_FAILED']
                retry=not failures or (at-timestamp(failures[0]['at'])).total_seconds()>=300
                if retry:
                    with Path(config['shared_executor_lock']).open('a') as cycle_lock:
                        try:fcntl.flock(cycle_lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
                        except BlockingIOError:return 0
                        journal=Journal(':memory:')
                        try:
                            with sqlite3.connect('file:'+str(state/'v1_state.sqlite')+'?mode=ro',uri=True) as source:source.backup(journal.db)
                            raw=Broker(adapter,expected_account_hash=config['account_hash']).read()
                            result=latest_result(base,local.date().isoformat());result['report_trading_authorized']=config.get('deployment_authorized') is True
                            if POLICY.get('strategy_b_core_cash') and not result.get('strategy_b_state'):
                                result.pop('phase',None)
                                result.pop('sector_regime',None)
                                try:
                                    from .history import signal
                                    from .strategy_b_cash import context
                                    result.update(context(signal(base/'data',now=at)))
                                except (ValueError,FileNotFoundError):pass
                            elif not POLICY.get('strategy_b_core_cash') and (not result.get('phase') or (POLICY.get('sector_defense') and not result.get('sector_regime'))):
                                result.pop('phase',None)
                                try:
                                    from .history import signal
                                    market=signal(base/'data',now=at)
                                    from .cycle import defense_reason
                                    period=POLICY.get('slow_ma_period',100);ma_key='sma'+str(period)
                                    defensive=defense_reason(market,use_sector=bool(POLICY.get('sector_defense')))
                                    result.update(phase='DEFENSIVE' if defensive else 'ATTACK',signal_date=market['date'],close=market['close'],sma50=market['sma50'],slow_ma_period=period,**{ma_key:market[ma_key]})
                                    if POLICY.get('sector_defense'):
                                        result.update(sector_regime=market['sector_regime'],defense_reason=defensive)
                                except (ValueError,FileNotFoundError):pass
                            try:result['report_trading_halted']=json.loads((state/'live_execution_state.json').read_text()).get('trading_halted')
                            except (OSError,ValueError):result['report_trading_halted']=None
                            last=store.db.execute("SELECT body,created_at FROM outbox WHERE kind='DECISION' AND created_at>=? AND created_at<=? ORDER BY created_at DESC LIMIT 1",
                                (local.replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc).isoformat(),
                                 at.astimezone(timezone.utc).isoformat())).fetchone()
                            if last and last['body'].startswith('ETF 决策') and len(last['body'].splitlines())>1:
                                stamp=timestamp(last['created_at']).astimezone(ET).strftime('%H:%M ET')
                                result['execution_receipt_label']=stamp+' · '+last['body'].splitlines()[1][:180]
                            text=holdings_report(journal,raw,result,at)
                            try:authorization_notice(store,recovered=True)
                            except sqlite3.Error:pass
                            from .telegram_reports import Observer
                            observer=Observer(state/'telegram-observation.sqlite')
                            try:
                                observer.observe(raw,result,at)
                                configure_sender(type('Sender',(),{'adapter':adapter})())
                                observer.drain(lambda body:sender(body,adapter))
                            finally:observer.db.close()
                            if due(at) and not store.message(key):
                                from tools.report_only import append_to
                                _,monitor=append_to('',{'quotes':raw['quotes'],'volatility_context':{}},at)
                                text += '\n' + compact_monitor(monitor.get('observation',{}))
                                store.resolve(key);store.enqueue(key,'DAILY_HOLDINGS',text);store.event(key,'REPORT_READY',dict(account_as_of=raw['as_of'],market_as_of=(result.get('market_trend') or {}).get('date'),monitor_status=monitor.get('status')))
                        except Exception as exc:
                            if authorization_required(exc):
                                try:authorization_notice(store)
                                except sqlite3.Error:pass
                            store.fault(key,category(exc));store.event(key,'READ_FAILED',dict(category=category(exc)))
                        finally:journal.db.close()
            try:drain(store,adapter,sender)
            except Exception as exc:store.fault('notification-worker:'+local.date().isoformat(),category(exc))
            assert not any(c in ('review_equity_order','place_equity_order') for c in adapter.transport.calls)
            return 0
        finally:store.close()
