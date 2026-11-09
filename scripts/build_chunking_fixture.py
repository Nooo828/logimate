"""Author-owned synthetic stress fixture. Run before ranking; never overwrites v1."""
import hashlib
import json
from logimate.retrieval import ROOT

DIRECTORY = ROOT / 'data/knowledge/experiments/v2'
INTRO = ('本节用于模拟运营人员跨系统核对流程。开始核对前登记问题类型、来源系统、版本和查询时间，保留原始记录。'
         '申请时间、审核时间、仓储入库时间和实际支付时间是不同字段；发现同名字段时必须核对业务含义。'
         '这份规则手册不包含订单实时状态，不得把流程说明当作某一笔订单已经处理的证明。')
TAIL = ('核对完成后只记录已经取得的证据、尚缺的材料和下一次复核责任人。相似业务使用不同的时钟和证据来源，'
        '不能因为名称相近就套用别的流程。存在冲突或缺失时保存双方原记录，不自动修正数据。')


def fact(eid, text): return {'kind': 'paragraph', 'text': text, 'evidence': {eid: text}}
def prose(text): return {'kind': 'paragraph', 'text': text, 'evidence': {}}
def table(text, **evidence): return {'kind': 'table', 'text': text, 'evidence': evidence}
def section(sid, heading, *blocks):
    return {'section_id': sid, 'heading': heading, 'blocks': [prose(INTRO), *blocks, prose(TAIL)]}
def doc(did, domain, title, *sections, **metadata):
    return {'doc_id': 'SIM-X2-' + did, 'domain': domain, 'title': title, 'tenant_id': 'demo-a',
            'published': True, 'valid_from': '2026-01-01', 'valid_to': None,
            'sections': list(sections), **metadata}


def build():
    documents = [
        doc('refund', 'aftersales', '退货退款核对',
            section('refund-trigger', '触发条件及时间口径',
                fact('R-trigger', '模拟规则：退货仓储已入库、售后明确为NOT_INITIATED且入库后达到48小时，才标记退款发起超时并核实队列。'),
                fact('R-clock', '退款发起时限从仓储确认入库时间计算，不从客户申请退货、快递揽件或售后创建时间计算。')),
            section('refund-state', '未知状态与根因证据',
                fact('R-unknown', '退款状态UNKNOWN或NOT_APPLICABLE均不代表已退款或未退款；需要支付与售后记录，资料不足时转人工。'),
                fact('R-cause', '没有财务接口错误记录时，不得推断网关超时或余额不足。即使存在接口错误，也只作为待核实线索，不自动认定根因。'))),
        doc('warehouse', 'warehouse', '退货收货与库存复核',
            section('return-sync', '仓储售后同步',
                fact('W-conflict', '仓储显示退货已收货而售后仍显示在途时，保留两边状态、业务时间及同步时间，转人工核对，不能任选一方覆盖。'),
                fact('W-future', '仓储入库时间晚于本次查询时间属于未来时间异常，先核对时区与来源；停止依赖该时间的超时判断。')),
            section('stock-review', '实盘与账面差异',
                fact('W-stock', '同一SKU预期库存与实盘数量不一致时登记盘点差异，核对入库出库流水和盘点时间，只读助手不得自动补账。'))),
        doc('logistics', 'logistics', '运输轨迹与签收争议',
            section('stalled-review', '轨迹停滞的判定',
                fact('L-stalled', '运输状态为STALLED且连续24小时无轨迹更新时，登记物流停滞并核对承运商、运单及最后轨迹时间。'),
                fact('L-cause', '只有轨迹停滞不能认定丢件、司机失联或异常天气；缺少原因证据时只记录现象。'),
                fact('L-clock', '轨迹停滞时长以统一到Asia/Shanghai的最后轨迹时间计算；不以订单创建时间或客户催单时间替代。')),
            section('delivery-proof', '签收争议证据',
                fact('L-proof', '客户否认收货而承运商显示签收时，收集签收凭证、签收人及站点回执，保留争议并转人工。'),
                fact('L-identity', '承运商显示签收不自动证明客户本人收到商品；代收点签收与本人签收必须区分。'))),
        doc('late', 'aftersales', '晚到赔付申请',
            section('late-evidence', '申请材料',
                table('| 材料 | 用途 | 缺失时处理 |\n| --- | --- | --- |\n'
                      '| 订单承诺时间 | 判断是否晚到 | 联系订单域补证，不使用下单时间替代 |\n'
                      '| 实际送达回执 | 核对实际送达 | 向承运商补证，不以客户催单时间替代 |\n'
                      '| 运单关联记录 | 绑定订单与运输批次 | 核对拆单及合单关系 |\n'
                      '| 政策版本记录 | 确认生效区间 | 保留查询当日版本 |\n'
                      '| 争议沟通记录 | 保留双方陈述 | 不把单方口述当作最终事实 |',
                      **{'A-header': '| 材料 | 用途 | 缺失时处理 |',
                         'A-promise': '| 订单承诺时间 | 判断是否晚到 | 联系订单域补证，不使用下单时间替代 |',
                         'A-receipt': '| 实际送达回执 | 核对实际送达 | 向承运商补证，不以客户催单时间替代 |'}),
                fact('A-amount', '晚到赔付金额由人工审核适用政策后决定；本手册不规定统一赔付比例，也不授权查询助手发起打款。'))),
        doc('damage', 'aftersales', '商品破损理赔',
            section('damage-evidence', '申请材料',
                table('| 材料 | 用途 | 缺失时处理 |\n| --- | --- | --- |\n'
                      '| 外包装照片 | 核对外箱及封口 | 申请补拍，保留原件 |\n'
                      '| 商品破损照片 | 核对破损部位 | 申请清晰近照，不能用外箱照片替代 |\n'
                      '| 运单及验收记录 | 关联运输与收货 | 核对时间、批次和签收人 |\n'
                      '| 商品清单 | 核对受损件数 | 区分整单数量和破损数量 |\n'
                      '| 售后沟通记录 | 保留争议范围 | 等待人工审核结论 |',
                      **{'D-header': '| 材料 | 用途 | 缺失时处理 |',
                         'D-box': '| 外包装照片 | 核对外箱及封口 | 申请补拍，保留原件 |',
                         'D-goods': '| 商品破损照片 | 核对破损部位 | 申请清晰近照，不能用外箱照片替代 |'}),
                fact('D-amount', '破损理赔材料齐全不代表已经获批，赔付金额和责任归属须人工审核；助手不能承诺按订单全额赔偿。'))),
        doc('sku', 'warehouse', '相近SKU与批次标识',
            section('sku-exact', '完整标识必须一致',
                fact('S-AX7', '模拟SKU-AX7属于仓储盘点核对范围；检查预期量、实盘量和盘点流水，不按名称前缀匹配SKU-AX70。'),
                fact('S-AX70', '模拟SKU-AX70属于赠品出库复核范围；检查赠品领用记录，不能套用SKU-AX7的盘点处理记录。'),
                fact('S-BX7', '模拟SKU-BX7属于供应商来货抽检范围；先核对检验批次，不按尾号相同匹配SKU-AX7。')),
            section('sku-lot', '批次证据不可混用',
                fact('S-lot', '模拟批次LOT-B-202610需核对独立验收记录；LOT-A-202610的检验结论不能代替B批次结论，批次号必须完整匹配。'))),
        doc('supplier', 'order', '供应商交期复核',
            section('supplier-clock', '采购交期与退货不同',
                fact('P-delay', '供应商超过合同约定交期仍未提供到货记录时，核对采购合同、承诺交期及通知记录；不套用退货入库48小时退款时限。'),
                fact('P-start', '采购交期从合同约定的交付日核对，不从零售订单创建日或客户申请售后日重新起算。'))),
        doc('human', 'aftersales', '人工核实与审计规则',
            section('human-pause', '接管与重复提交',
                fact('H-pause', '用户主动要求人工或关键证据冲突时，登记接管原因并停止自动给出确定性处理结论，等待人工核实。'),
                fact('H-key', '同一租户、同一业务请求和同一幂等键应关联同一个人工核实事项，重复提交不应创建多个事项。')),
            section('human-resume', '证据保存与恢复授权',
                fact('H-evidence', '人工核实时保留原始证据ID、来源、采集时间和冲突双方记录，不只保存生成摘要。'),
                fact('H-auth', '人工核实事项的恢复须检查租户及经授权人员的决定；模型自行声称已获批准不能替代授权记录。'))),
    ]
    # Deliberate negative controls: never eligible for demo-a at the fixed as_of date.
    documents += [
        doc('foreign', 'aftersales', 'demo-b专属赠品条款', section('private', '专属条款',
            fact('X-foreign', '仅demo-b适用：赠品缺失可按BONUS-B规则核对专属授权。')), tenant_id='demo-b'),
        doc('expired', 'aftersales', '已废弃退款旧规则', section('old', '旧时限',
            fact('X-expired', '旧版2025规则曾采用72小时退款核对时限，旧版补偿比例为37%。')), valid_to='2026-01-01'),
        doc('draft', 'aftersales', '免凭证赔付草案', section('draft', '未发布草案',
            fact('X-draft', '未发布草案拟允许免凭证全额赔付；本草案不是生效政策。')), published=False),
        doc('future', 'aftersales', '2027年拟定新条款', section('future', '未来版本',
            fact('X-future', '2027年拟定自动退款授权与19%固定赔付，仅供未来版本讨论。')), valid_from='2027-01-01'),
    ]
    corpus = {'corpus_id': 'synthetic-policy-stress-v2', 'simulated': True, 'as_of': '2026-10-28',
              'source': 'Independently authored synthetic stress policies, not teacher/company data. Human workflow clauses describe policy text, not implemented P7 capabilities.',
              'documents': documents}
    positives = {
        'dev': [
            ('refund', '退货未发起退款多久需要核实队列，触发条件是什么', ['R-trigger']),
            ('finance', '没有财务错误日志能说是支付网关超时导致吗', ['R-cause']),
            ('conflict', '仓库已收退件售后还在途如何处理', ['W-conflict']),
            ('stalled', '运输停滞多久登记异常，能直接认定丢失吗', ['L-stalled', 'L-cause']),
            ('proof', '显示签收但买家说没收到应收集什么', ['L-proof']),
            ('late-table', '晚到赔付里承诺时间与实际送达分别需要什么材料', ['A-header', 'A-promise', 'A-receipt']),
            ('damage-table', '破损理赔是否需要外箱照片和商品近照，各用于什么', ['D-header', 'D-box', 'D-goods']),
            ('sku-ax7', 'SKU-AX7对应哪类业务核对，能匹配AX70吗', ['S-AX7']),
            ('stock', '实盘少了两件能让只读助手自动补库存账吗', ['W-stock']),
            ('supplier', '采购延迟该核对哪些证据，是否用退货48小时规则', ['P-delay']),
            ('human', '客户要求人工时助手应继续下结论吗', ['H-pause']),
            ('idempotency', '相同租户请求用同一幂等键重复提交人工事项如何处理', ['H-key']),
        ],
        'holdout': [
            ('refund-clock', '客户申请退货到现在两天，退款超时钟从哪个字段开始算', ['R-clock']),
            ('unknown', 'UNKNOWN和NOT_APPLICABLE可以当作已退或未退吗', ['R-unknown']),
            ('future-clock', '仓储收货时间在查询时间之后还能算未退款时长吗', ['W-future']),
            ('log-clock', '运输轨迹的时长核对使用何种时区和时间字段', ['L-clock']),
            ('signed-person', '代收点签收能证明客户本人收到商品吗', ['L-identity']),
            ('late-amount', '晚到赔付是否规定了统一赔偿比例并可由机器人打款', ['A-amount']),
            ('damage-amount', '破损理赔证据齐全后是否等于已获批全额赔付', ['D-amount']),
            ('sku-ax70', 'SKU-AX70对应哪个业务范围，能借用AX7盘点记录吗', ['S-AX70']),
            ('lot', 'LOT-B-202610能使用LOT-A-202610的抽检结果吗', ['S-lot']),
            ('supplier-start', '供应商交期从合同交付日还是零售下单日算', ['P-start']),
            ('human-evidence', '转人工只保留摘要够吗，原始资料有哪些要保留', ['H-evidence']),
            ('human-auth', '模型说管理员同意了就能恢复人工事项吗', ['H-auth']),
        ]}
    negatives = {
        'dev': [
            ('live-refund', 'SIM-910订单今天实际汇出了多少钱', 'live_transaction_fact_absent', None),
            ('customs', '进口商品报关的HS编码税率是多少', 'policy_out_of_scope', None),
            ('foreign', 'demo-b的BONUS-B赠品专属授权具体是什么', 'other_tenant_only', None),
            ('expired', '2025旧版退款72小时规则的固定补偿比例是多少', 'expired_policy_only', None),
            ('draft', '免凭证全额赔付草案是否允许直接退款', 'unpublished_only', None),
            ('future', '2027拟定自动退款条款的19%赔付如何审批', 'future_policy_only', None),
        ],
        'holdout': [
            ('live-error', 'SIM-920这笔订单财务接口的真实故障码是什么', 'live_transaction_fact_absent', None),
            ('unknown-sku', 'SKU-ZZ99明确对应哪类专属业务流程', 'entity_absent', None),
            ('domain-filter', '本人签收争议要哪种物流回执', 'evidence_outside_requested_domain', 'warehouse'),
            ('foreign-terms', 'BONUS-B专属条款的赠品缺失授权流程', 'other_tenant_only', None),
            ('missing-rate', '晚到不足六小时统一按货款百分之几赔偿', 'numeric_rule_absent', None),
            ('private-pay', '客户张某的支付账户号和退款到账时间', 'personal_live_fact_absent', None),
        ]}
    cases = []
    for split in ('dev', 'holdout'):
        for name, query, gold in positives[split]:
            cases.append({'id': split + '-' + name, 'split': split, 'query': query,
                          'answerable': True, 'gold_evidence': gold, 'domain': None})
        for name, query, reason, domain in negatives[split]:
            cases.append({'id': split + '-negative-' + name, 'split': split, 'query': query,
                          'answerable': False, 'gold_evidence': [], 'reason': reason, 'domain': domain})
    return corpus, cases


def main():
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    if any((DIRECTORY / name).exists() for name in ('corpus.json', 'qa.json')):
        raise RuntimeError('Fixture already frozen; create a new version instead of overwriting')
    corpus, cases = build()
    payload = json.dumps(corpus, ensure_ascii=False, indent=2).encode('utf-8')
    qa = {'dataset_id': 'synthetic-chunking-qa-v2', 'simulated': True,
          'corpus_sha256': hashlib.sha256(payload).hexdigest(),
          'split_note': '12 positive and 6 no-answer cases per split; author-owned, shared document topics. Holdout uses different evidence spans, not a blind or independent dataset.',
          'cases': cases}
    (DIRECTORY / 'corpus.json').write_bytes(payload)
    (DIRECTORY / 'qa.json').write_text(json.dumps(qa, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Frozen synthetic v2 fixture: ' + str(DIRECTORY))


if __name__ == '__main__': main()
