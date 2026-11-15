"""Presentation and durable read-only account observation; never executes orders."""
import hashlib,json,sqlite3
from datetime import date,datetime,timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from .cycle import POLICY,account_symbols,core_symbols
ET=ZoneInfo('America/New_York')
DEFENSE_LABELS={'QQQ_AND_SMH_BEAR':'QQQ 转弱、半导体熊市',
                'QQQ_TREND_DEFENSE':'QQQ 长期趋势转弱','SMH_SECTOR_BEAR':'半导体熊市'}
BROAD_LABELS={'BULL_CONFIRMED':'偏多一致','BEAR_CONFIRMED':'偏空一致','DIVERGENT':'走势分化'}
def usd(value):
    try:
        n=Decimal(str(value))
        return '$'+format(n,'.2f') if n.is_finite() and n>=0 else '待核实'
    except Exception:return '待核实'
def trend(result):
    if result.get('strategy_b_state'):
        from .strategy_b_cash import STATE_LABELS
        return STATE_LABELS.get(result['strategy_b_state'],'待核实')
    phase=result.get('phase')
    if sector := result.get('sector_regime'):
        if phase=='DEFENSIVE':
            return '防御 · '+DEFENSE_LABELS.get(result.get('defense_reason'),'趋势防御')
        if phase=='ATTACK':
            return '进攻 · QQQ／VOO '+BROAD_LABELS[sector['broad_view']]
    return '均线偏多' if phase=='ATTACK' else '均线转防御' if phase=='DEFENSIVE' else '待核实'


def market_position(result):
    from .cycle import POLICY
    period=result.get('slow_ma_period',POLICY.get('slow_ma_period',200))
    try:
        close,slow=(Decimal(str(result[k])) for k in ('close','sma'+str(period)))
        if not (close.is_finite() and slow.is_finite() and min(close,slow)>0):
            raise ValueError('unverified_close')
        relative=(close/slow-1)*100
        return f'短线位置｜QQQ {result["signal_date"]} 收盘距 {period} 日线 {relative:+.2f}%'
    except (KeyError,ValueError,ArithmeticError):
        return '短线位置｜待核实'


def execution_summary(result):
    if not result.get('run_at'):
        return '当日尚无已核实的执行记录'
    return '；'.join(decision(result).splitlines()[1:])
REASONS = {
    'WAIT_STRATEGY_B_CASH': '可用自有现金低于券商最低额',
    'WAIT_STRATEGY_B_NO_CORE_ORDER': '本轮没有达到最低额的核心买单',
    'WAIT_PENDING_DEPOSIT': '等待入金结算',
    'BLOCKED_BY_UNRESOLVED_INTENT': '已有订单待对账',
    'WAIT_PENDING_APPROVAL': '请在 Robinhood 查看审批',
    'pending_broker_approval': '请在 Robinhood 查看审批',
    'unresolved_intent': '已有订单待对账',
    'fresh_plan_changed': '复核时计划已变化',
    'NO_ELIGIBLE_ORDER': '暂无符合条件的交易',
    'WAIT_POLICY_OR_SESSION_USED': '暂无可执行订单',
    'WAIT_DAILY_BUY_LIMIT': '当日买入额度已用完',
    'WAIT_WINDOW_BUY_LIMIT': '本轮剩余额度低于券商最低额',
    'WAIT_INTRADAY_FILTER': '分钟择时条件未通过',
    'STOP_LOSS_6_6_ALL_TQQQ': 'TQQQ 触及止损',
    'STOP_LOSS_FILLED_WAIT_SETTLEMENT': 'TQQQ 止损成交，等待结算',
    'TREND_EXIT': '趋势退出',
    'HARVEST_ABOVE35_TO30': '超额减仓',
    'portfolio_value_unreconciled': '券商账户估值待对账',
    'FILLED_VALUATION_PENDING': '已成交，券商估值稍后同步',
}


def day_fill_lines(result):
    """Ledger fills of the day; shown when this run itself has no executions."""
    if result.get('executions') or not result.get('day_fills'):
        return []
    parts = [('买入 ' if f['side']=='BUY' else '卖出 ') + f['symbol'] + ' ' + usd(f['gross'])
             for f in result['day_fills']]
    return ['今日已成交｜' + ' · '.join(parts)]
EXECUTION_LABELS = {
    'SUBMITTED': '已提交 · 待成交',
    'OPEN': '已提交 · 待成交',
    'PARTIALLY_FILLED': '部分成交',
    'FILLED': '已成交',
    'AWAITING_USER_APPROVAL': '待审批 · 请打开 Robinhood',
    'EXECUTION_UNKNOWN': '订单状态待核查',
    'UNKNOWN': '订单状态待核查',
    'REJECTED': '券商拒绝',
    'CANCELED': '已撤销',
    'CANCEL_PENDING': '撤销待确认',
    'WAIT': '未提交',
}


def reason_text(reason):
    code = str(reason or '')
    if code.startswith('TRADING_HALTED'):
        return '交易暂停'
    if any(term in code.lower() for term in ('401','unauthorized','auth_required')):
        return '请重新连接 Robinhood'
    key = code.rsplit(':',1)[-1]
    return REASONS.get(key, '数据或执行待核实' if ':' in code else '按策略等待')


def wait_reason(result):
    if result.get('reason') in ('WAIT_POLICY_OR_SESSION_USED','WAIT_STRATEGY_B_CASH') and result.get('cash_basis')=='VERIFIED_CURRENT':
        try:
            cash,minimum=(Decimal(str(value)) for value in (result.get('safe_cash'),POLICY['broker_minimum']))
            if cash.is_finite() and 0<=cash<minimum:
                return f'可用自有资金 {usd(cash)}，低于券商最低买入额 {usd(minimum)}'
        except (KeyError,ArithmeticError):
            pass
    return reason_text(result.get('reason'))


def decision(result):
    executions = result.get('executions') or []
    selected = result.get('selected')
    reason = result.get('reason')
    header = 'ETF 决策'
    if result.get('run_at'):
        header += '｜' + datetime.fromisoformat(result['run_at']).astimezone(ET).strftime('%H:%M ET')
    lines = [header]
    fills = result.get('window_fills') or []
    if not fills:
        lines += day_fill_lines(result)
    for fill in fills:
        side = '买入' if fill['side']=='BUY' else '卖出'
        lines.append(f"{side} {fill['symbol']} {usd(fill['gross'])} · 已成交")
    if fills and not selected and (not executions or result.get('cash_basis')=='VERIFIED_CURRENT'
                                  or reason=='FILLED_VALUATION_PENDING'):
        lines.append('最终结果｜'+wait_reason(result))
    if not executions and not fills:
        if selected:
            side = '买入' if selected['side']=='BUY' else '卖出'
            lines.append(f"计划{side} {selected['symbol']} {usd(selected.get('amount'))} · 尚未提交")
        else:
            from .notifications import authorization_required
            lines.append('WAIT · Robinhood 授权待恢复' if authorization_required(reason) else 'WAIT · ' + wait_reason(result))
    for item in executions:
        if item['execution']['status']=='FILLED' and any(
                (f['symbol'],f['side'])==(item['plan']['symbol'],item['plan']['side']) for f in fills):
            continue
        order, execution = item['plan'], item['execution']
        side = '买入' if order['side']=='BUY' else '卖出'
        status = execution['status']
        line = f"{side} {order['symbol']}｜{EXECUTION_LABELS.get(status, '状态 ' + status)}"
        if status == 'WAIT':
            line += ' · ' + reason_text(execution.get('reason'))
        elif order.get('amount') is not None and status != 'FILLED':
            line += ' · 计划 ' + usd(order['amount'])
        if order.get('reason') in ('STOP_LOSS_6_6_ALL_TQQQ','TREND_EXIT','HARVEST_ABOVE35_TO30'):
            line += ' · ' + reason_text(order['reason'])
        lines.append(line)
    if executions and (reason in ('WAIT_PENDING_DEPOSIT','WAIT_PENDING_APPROVAL','BLOCKED_BY_UNRESOLVED_INTENT')
                       or (not fills and reason=='FILLED_VALUATION_PENDING')):
        lines.append(reason_text(reason))
    if reason == 'WAIT_PENDING_DEPOSIT':
        lines.append('待到账入金：'+usd(result.get('pending_external_credits'))+
                     '｜账户现金：'+usd(result.get('reported_cash'))+'｜可用自有资金：待核实')
    return '\n'.join(lines)


DETAIL_BREAK = '\n\n详情｜点开展开\n'

INTRADAY_LABELS = {
    'INTRADAY_STABILIZED': '短线止跌、量差合格',
    'INTRADAY_ABOVE_VWAP_BAND': '高于估算 VWAP 允许范围',
    'INTRADAY_QUOTE_MOVED': '报价偏离已完成 K 线',
    'INTRADAY_NOT_STABILIZED': '5 分钟走势尚未止跌',
    'INTRADAY_FALLING_15M': '15 分钟跌幅过大',
    'INTRADAY_VOLUME_THIN': '相对成交量不足',
    'INTRADAY_SPREAD_WIDE': '买卖价差偏大',
}


def voo_fundamentals(path, day):
    if path is None:
        return None
    try:
        row = json.loads(path.with_name('voo-fundamentals.json').read_text())
        as_of = date.fromisoformat(row['as_of'])
        pe, pb = (Decimal(str(row[key])) for key in ('pe','pb'))
        if (row['symbol'] == 'VOO' and row['provider'] == 'Vanguard'
                and 0 <= (day-as_of).days <= 130
                and all(value.is_finite() and value > 0 for value in (pe,pb))):
            return f'VOO 官方估值｜市盈率 {pe:.1f}× · 市净率 {pb:.1f}×（{as_of:%Y-%m-%d}）'
    except (OSError, ValueError, ArithmeticError, TypeError, KeyError):
        pass
    return None


def valuation_reference(path, now):
    primary = core_symbols()[1]
    day = now.astimezone(ET).date()
    official = voo_fundamentals(path, day) if primary == 'VOO' else None
    official_detail = 'VOO 估值来源｜Vanguard 季度基金资料 · 情景估值待核实' if official else None
    unavailable = ('情景估值｜待更新'+('\n'+official if official else ''), official_detail)
    if path is None:
        return unavailable
    try:
        snapshot = json.loads(path.read_text())
        holdings_day = date.fromisoformat(snapshot['as_of'])
        market_day = date.fromisoformat(snapshot['market_as_of'])
        available_at = datetime.fromisoformat(snapshot['available_at'])
        if not (holdings_day <= market_day <= day and (day-holdings_day).days <= 7
                and available_at.tzinfo is not None and available_at <= now):
            return unavailable
        centers = []
        for symbol in (primary,'SMH'):
            row = snapshot['symbols'].get(symbol)
            if row is None:
                if symbol != 'VOO' or not official:
                    centers.append(f'{symbol} 待核实')
                continue
            center, coverage = (Decimal(str(row[key])) for key in ('center','cashflow_coverage'))
            if not (center.is_finite() and center>0 and coverage.is_finite() and 85<=coverage<=100):
                return unavailable
            centers.append(f'{symbol} ${center:.0f}')
            if symbol == 'VOO':
                official_detail = None
    except (OSError, ValueError, ArithmeticError, TypeError, KeyError):
        return unavailable
    summary = '情景估值｜'+(' · '.join(centers) or '待更新')
    detail = f'估值依据｜成分股 {holdings_day:%m-%d} · 收盘 {market_day:%m-%d} · 仅报告'
    if official:
        summary += '\n'+official
    if official_detail:
        detail += '\n'+official_detail
    return summary, detail


def decision_notification(result, data_directory, now, *, valuation_path=None):
    from .price_levels import build, display_symbols, render

    lines = []
    period = result.get('slow_ma_period', POLICY.get('slow_ma_period',100))
    if result.get('signal_date'):
        lines.append(f"趋势参考 {result['signal_date']}｜QQQ SMA50 {usd(result.get('sma50'))}"
                     f" / SMA{period} {usd(result.get(f'sma{period}'))}")
    if sector := result.get('sector_regime'):
        broad=BROAD_LABELS[sector['broad_view']]
        lines.append(f"QQQ／VOO 趋势｜{broad}")
        lines.append(f"半导体｜{'熊市' if sector['smh_bear'] else '未触发'} · SMH 距200日线 {sector['smh_distance_pct']:+.2f}%"
                     f" · 63日相对 VOO {sector['smh_relative_63_pct']:+.2f}%")
    daily_limit = POLICY.get('daily_buy_limit')
    window_limit = POLICY.get('per_window_buy_limit')
    funds = ('下单前可用自有资金' if result.get('cash_basis')=='PRE_ORDER' else '可用自有资金')
    lines.append(f"{funds}｜{usd(result.get('safe_cash'))}")
    lines.append('资金规则｜已核实自有资金' + (
        f' · 每日 {usd(daily_limit)} · 每窗口 {usd(window_limit)}'
        if daily_limit is not None and window_limit is not None else
        f' · 每日 {usd(daily_limit)}' if daily_limit is not None else ' · 无固定日／窗口额度'))
    if result.get('window_allocation') is not None:
        lines.append('分配｜按目标缺口')
    if result.get('strategy_b_state'):
        row = result['cash_weights']
        lines.append('新增资金分配｜'+ ' · '.join(f'{s} {Decimal(row[s]):.0%}' for s in ('VOO','SMH')))
        if allocation := result.get('core_allocation'):
            lines.append('纠偏后本日计划｜'+ ' · '.join(f'{s} {usd(allocation[s])}' for s in ('VOO','SMH')))
        lines.append('风险信号｜'+(' · '.join(result['risk_signals']) or '未触发'))
    for symbol, reading in result.get('intraday',{}).items():
        reasons = ' · '.join(INTRADAY_LABELS.get(code,code) for code in reading.get('reasons',[]))
        lines.append(f"{symbol} 分钟｜{reading.get('decision','待核实')}" + (' · '+reasons if reasons else ''))
    selected = result.get('selected')
    orders = result.get('executions') or ([{'plan':selected,'execution':{}}] if selected else [])
    for item in orders:
        plan, execution = item['plan'], item['execution']
        status = execution.get('status','NOT_SUBMITTED')
        fields = [f"{plan['side']} {plan['symbol']}", '计划 '+usd(plan.get('amount')),
                  '参考价 '+usd(plan.get('price')), EXECUTION_LABELS.get(status,'尚未提交' if status=='NOT_SUBMITTED' else status)]
        if plan.get('quantity') is not None:
            fields.append(str(plan['quantity'])+' 股')
        if status in ('EXECUTION_UNKNOWN','UNKNOWN'):
            fields.append('禁止重复提交')
        if adjustment := plan.get('constituent_breadth_adjustment'):
            fields.append('前十大技术过滤 · 原计划 '+usd(adjustment['original_amount'])+' · 本次减半')
        codes = [str(reason) for reason in (plan.get('reason'),execution.get('reason')) if reason]
        if codes:
            fields.append('依据 '+ ' / '.join(codes))
        lines.append('｜'.join(fields))
    safety = result.get('tqqq_protection') or {}
    if POLICY.get('strategy_b_core_cash'):
        lines.append('TQQQ｜战术目标 0% · 禁止下单')
    elif not POLICY.get('tqqq_stop_loss_enabled',True):
        exit_label='防御模式退出' if POLICY.get('sector_defense') else f'SMA50/{period} 趋势退出'
        lines.append(f'TQQQ 风控｜{exit_label} · 超额减仓 · 固定价格止损未启用')
    elif safety.get('stop_price') is not None:
        lines.append('TQQQ 止损线｜'+usd(safety['stop_price']))
    if result.get('reason'):
        lines.append('本轮原因码｜'+str(result['reason']))
    try:
        levels = build(data_directory, now)
    except Exception:
        levels = {}  # Price references are optional even when a decision must be sent.
    references = []
    for symbol in display_symbols():
        row = levels.get('symbols', {}).get(symbol, {})
        if row.get('status') == 'OK':
            references.append(f"{symbol} {usd(row['support'])}/{usd(row['breakout'])}")
        else:
            references.append(f'{symbol} 待更新')
    signal_day=' · '+result['signal_date'] if result.get('signal_date') else ''
    label='当前模式｜' if POLICY.get('sector_defense') or POLICY.get('strategy_b_core_cash') else '中期趋势｜'
    summary = (decision(result) + '\n'+label+trend(result)+signal_day+'\n'+market_position(result)
               +'\n支撑／突破｜'+' · '.join(references))
    valuation, valuation_details = valuation_reference(valuation_path,now)
    if valuation_details:
        lines.append(valuation_details)
    return summary + '\n' + valuation + DETAIL_BREAK + '\n'.join(lines) + '\n\n' + render(levels)


def fill_message(order,fill,raw,regime):
    price = Decimal(str(fill['price']))
    quantity = Decimal(str(fill['quantity']))
    side = '买入' if order['side'].upper() == 'BUY' else '卖出'
    time = datetime.fromisoformat(fill['timestamp']).astimezone(ET).strftime('%m-%d %H:%M:%S ET')
    return f"ETF 账户成交｜{side} {order['symbol']} {usd(price*quantity)}\n{quantity} 股 × {usd(price)}｜{time}"

def safe_cash(raw):
    from .funds import normalize
    try:return usd(normalize(raw)['safe_cash'])
    except (ValueError,KeyError):return '待核实'
def portfolio_symbols(raw):
    smh,primary=core_symbols()
    symbols=(primary,smh,'TQQQ')
    return (*symbols,'QQQ') if primary!='QQQ' and Decimal(str(raw['positions']['QQQ']['quantity'])) else symbols
def daily(raw,result,at):
    total=Decimal(str(raw['reported_net_assets']));cash=Decimal(str(raw['funds']['reported_cash']))
    lines=['📊 ETF Agent | 每日持仓 | '+at.astimezone(ET).date().isoformat(),'市场行情：'+trend(result),'Robinhood 现金：'+usd(cash)]
    symbols = portfolio_symbols(raw)
    for symbol in symbols:
        q=Decimal(str(raw['positions'][symbol]['quantity']));quote=raw['quotes'][symbol];price=Decimal(str(quote['last']));value=q*price
        pct=format(value/total*100,'.2f')+'%' if total>0 else '待核实'
        lines.append(symbol+'：'+format(q,'.6f')+' 股 | '+usd(value)+' | '+pct+' | 现价 '+usd(price))
    lines+=['现金：'+usd(cash)+' | 总资产（券商）：'+usd(total),'可用自有资金：'+safe_cash(raw),
        '未完成订单：'+('是' if any(o.get('state') not in ('filled','canceled','cancelled','rejected','failed','expired','voided','partially_filled_rest_cancelled') for o in raw['orders']) else '否'),
        '查询：'+at.astimezone(ET).strftime('%Y-%m-%d %H:%M ET'),'报价：'+'；'.join(s+' '+datetime.fromisoformat(raw['quotes'][s]['timestamp'].replace('Z','+00:00')).astimezone(ET).strftime('%m-%d %H:%M') for s in symbols),
        '只读报告；报价按所列时间，现金不等于可用资金。']
    return '\n'.join(lines)
class Observer:
    def __init__(self,path):
        self.db=sqlite3.connect(path,timeout=1,isolation_level=None)
        self.db.executescript('CREATE TABLE IF NOT EXISTS tg_meta(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE IF NOT EXISTS tg_seen(id TEXT PRIMARY KEY,facts TEXT); CREATE TABLE IF NOT EXISTS tg_outbox(key TEXT PRIMARY KEY,body TEXT,state TEXT,message_id INTEGER);')
    def observe(self,raw,result,at,*,daily_due=False,covered_fills=(),deferred_fills=()):
        if raw.get('complete_orders') is not True:raise ValueError('notification_orders_incomplete')
        account=raw['account_hash'];bound=self.db.execute("SELECT value FROM tg_meta WHERE key='account'").fetchone()
        if bound and bound[0]!=account:raise ValueError('notification_account_changed')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            first=bound is None
            if first:self.db.execute("INSERT INTO tg_meta VALUES('account',?)",(account,))
            for order in raw['orders']:
                if order.get('symbol') not in account_symbols():continue
                for fill in order.get('executions',[]):
                    key=str(order['id'])+':'+str(fill['id']);facts=json.dumps([order['symbol'],order['side'],fill],sort_keys=True)
                    known=self.db.execute('SELECT facts FROM tg_seen WHERE id=?',(key,)).fetchone()
                    if known and known[0]!=facts:raise ValueError('notification_fill_conflict')
                    if known:continue
                    price=Decimal(str(fill['price']));qty=Decimal(str(fill['quantity']));when=datetime.fromisoformat(fill['timestamp'].replace('Z','+00:00'))
                    if not price.is_finite() or not qty.is_finite() or price<=0 or qty<=0 or when.tzinfo is None or when>at or fill.get('currency')!='USD':raise ValueError('notification_fill_invalid')
                    if not first and key in deferred_fills and key not in covered_fills:continue
                    self.db.execute('INSERT INTO tg_seen VALUES(?,?)',(key,facts))
                    if not first and key not in covered_fills:self.enqueue('fill:'+key,fill_message(order,fill,raw,trend(result)))
            if daily_due:self.enqueue('holdings:'+at.astimezone(ET).date().isoformat(),daily(raw,result,at))
            self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise
    def enqueue(self,key,text):self.db.execute("INSERT OR IGNORE INTO tg_outbox VALUES(?,?,'PENDING',NULL)",(key,text))
    def drain(self,sender):
        # Atomic claim prevents duplicate sends across concurrent processes.
        # A crashed/uncertain send stays SENDING/UNKNOWN and is never retried.
        for key,body in self.db.execute("SELECT key,body FROM tg_outbox WHERE state='PENDING' ORDER BY rowid").fetchall():
            if self.db.execute("UPDATE tg_outbox SET state='SENDING' WHERE key=? AND state='PENDING'",(key,)).rowcount!=1:continue
            try:
                receipt=sender(body);mid=receipt.get('message_id')
                if receipt.get('state')=='SENT' and type(mid) is int and mid>0:state='SENT'
                else:state='REJECTED' if receipt.get('state')=='REJECTED' else 'UNKNOWN';mid=None
            except Exception:state='UNKNOWN';mid=None
            self.db.execute('UPDATE tg_outbox SET state=?,message_id=? WHERE key=?',(state,mid,key))
