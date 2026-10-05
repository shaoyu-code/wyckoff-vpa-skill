"""Source-located, paraphrased rule cards: initial verified coverage, not full books.

These constraints inform an evidence reviewer; they are not numeric trading rules.
Book files are not distributed. Original-text/translator differences remain explicit.
"""
from copy import deepcopy
from .tradingview import NativeError
from ..contracts import digest

BOOKS = {
 'B1': {'title':'威科夫操盘法：华尔街大师成功驾驭市场超过95年的秘技',
        'sha256':'ad97334e6b0a559dd1f98dad3e45149a6a97772a4d8ceaa6435f3c3cd88f03dd'},
 'B2': {'title':'擒庄秘籍：威科夫量价交易技术实战教程',
        'sha256':'704ca09a3c40f7137ec7a9b72b76bb3a3f65c2bdfc4cdce559a778681cf48b32'},
 'B3': {'title':'新威科夫操盘法：揭秘对冲基金不愿公开的交易策略',
        'sha256':'a4cc67a1bec2b961a03ca2c8bf40cbe9d83eba4ebace30469f58283ea839fb07'},
}
RULES = [
 {'id':'READ_ORDER','source':[{'book':'B1','locator':'第一章第一节；OEBPS/text00004.html'},
    {'book':'B3','pdf_pages':[22,23],'printed_pages':[2,3]}],
  'principle':'先研究背景和价量，再解释其性质，最后形成判断和应对。',
  'precondition':'实际量价和相关背景可读。','cannot_infer':'不能先认一个形状再寻找支持性引文。'},
 {'id':'WAVE_COMPARISON','source':[{'book':'B2','pdf_pages':[43,44],'printed_pages':[31,32]}],
  'principle':'比较买盘波与卖盘波的持续时间、速度和运行幅度，理解力量变化。',
  'precondition':'明确两段的范围、时间和价格端点，结合成交量。',
  'cannot_infer':'某一比值不是确定的多空结论，更不是发生概率。'},
 {'id':'SC_MULTI_BAR','source':[{'book':'B2','pdf_pages':[51],'printed_pages':[39]}],
  'principle':'恐慌抛售可以跨多日；最大成交量和最低价格不必同日。',
  'precondition':'下降背景中的相关行为与后续响应；允许不同表现形式。',
  'cannot_infer':'最大量阴线不自动成为SC，也不自动说明吸筹完成。'},
 {'id':'SPRING_HIGH_VOLUME','source':[{'book':'B3','pdf_pages':[44,45],'printed_pages':[24,25],'figure':'1.10'}],
  'principle':'放量下冲收回仍可进入Spring分析，但供应可能仍在，须观察二次测试。',
  'precondition':'先前有效支撑、实际下冲与返回，以及后续供应响应。',
  'cannot_infer':'不能以低量为所有Spring的唯一入口；收回不自动构成进场许可。'},
 {'id':'SOS_INSIDE_RANGE','source':[{'book':'B3','pdf_pages':[90,91,92,93],'printed_pages':[70,71,72,73],'figure':'2.9–2.11'}],
  'principle':'SOS可发生在区间内，并可由持续上升波构成；越过关键阻力时另说明JOC。',
  'precondition':'有持续需求表现及相应背景；后续测试能验证或否定有效性。',
  'cannot_infer':'不能把所有SOS限定为突破箱顶的一根阳线，也不能无视测试时供应增强。'},
 {'id':'STOP_NOT_REVERSAL','source':[{'book':'B3','pdf_pages':[76,77],'printed_pages':[56,57]}],
  'principle':'停止行为提示供求改变，不等于趋势立即反转。',
  'precondition':'位置、量价努力结果与后续跟随均需观察。',
  'cannot_infer':'出现一根上影或下影不能自动确认完整顶部或底部。'},
 {'id':'WEAK_DEMAND_NOT_SUPPLY','source':[{'book':'B3','pdf_pages':[31,60,61],'printed_pages':[11,40,41]}],
  'principle':'需求疲弱和供应持续扩大应分开观察；前者单独出现不证明后者。',
  'precondition':'区分上涨疲弱与回落中压力真正增加。',
  'cannot_infer':'缩量上涨不能直接判作派发完成。'},
 {'id':'EARLY_ENTRY_DISAGREEMENT','source':[{'book':'B2','pdf_pages':[52],'printed_pages':[40]}],
  'principle':'正文讨论早期买入；译者指出当时停止尚未确认，认为风险较高。',
  'precondition':'原文和译者意见并列，注明采用何种前提。',
  'cannot_infer':'不能把两种意见拼成一条无条件买入规则。'},
 {'id':'PREFIX_REPLAY','source':[{'book':'B2','pdf_pages':[50],'printed_pages':[38]}],
  'principle':'遮住后续走势，由左向右分析当时的量价。',
  'precondition':'所需信息在该判断时间已经可用。',
  'cannot_infer':'后续确认的拐点不能回填成此前已发出的判断。'},
 {'id':'TIMEFRAME_BACKGROUND','source':[{'book':'B2','pdf_pages':[23,47],'printed_pages':[11,35]},
    {'book':'B3','pdf_pages':[53,54,67],'printed_pages':[33,34,47]}],
  'principle':'高周期用于长期位置；较小周期可展开局部准备过程。总体底和局部底有不同背景。',
  'precondition':'各周期身份、所属范围及背景关系明确。',
  'cannot_infer':'不能将日线标签直接复制为4H已确认结构；3小时图例不是强制4H规则。'},
]
BY_ID = {r['id']:r for r in RULES}


def packet_rules():
    return {'books':deepcopy(BOOKS),'cards':deepcopy(RULES),'priority':'EQUAL_NO_PRIMARY_BOOK',
            'coverage':'INITIAL_VERIFIED_CARDS_NOT_FULL_TEXT_COVERAGE','version':digest(RULES),
            'status':'METHOD_REFERENCES_NOT_MARKET_ACCURACY_CERTIFICATE'}


def validate_rule_bindings(bindings, bars, *, required=False):
    if not isinstance(bindings,list) or (required and not bindings):
        raise NativeError('BOOK_BASIS_REQUIRED')
    indices={b['bar_index'] for b in bars}
    for b in bindings:
        if b.get('rule_id') not in BY_ID or not b.get('application') or not b.get('counterevidence'):
            raise NativeError('UNKNOWN_OR_EMPTY_RULE_BINDING')
        refs=b.get('bar_indices',[])
        if not refs or len(set(refs))!=len(refs) or any(type(i) is not int or i not in indices for i in refs):
            raise NativeError('RULE_BINDING_BAR_NOT_IN_EVIDENCE')
    return deepcopy(bindings)
