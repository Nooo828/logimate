"""Author-owned synthetic policy corpus and paraphrase split; not enterprise documents."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RULES = [
    ('refund', 'aftersales', '退货退款', '退款发起时限', '模拟规则：退货已经仓储入库且售后明确为未发起退款，达到48小时应核实退款队列。财务接口错误只能作为线索；无错误记录不能编造财务根因。', '退货进仓两天仍没退款应该检查什么', '退回商品已入库超过48小时但还没发起退钱，如何核对'),
    ('unknown', 'aftersales', '退款状态', '未知状态处理', '模拟规则：退款状态为unknown或not_applicable时，不能按未退款或已退款处理。先核实售后与支付记录，资料不足转人工。', '退款状态unknown能直接算未退吗', '系统说退款不适用，是否能断定钱已退回'),
    ('conflict', 'warehouse', '退货核对', '跨系统冲突', '模拟规则：仓库显示退货已收货，售后仍显示在途，属于跨系统状态冲突。保留双方记录和同步时间，转人工核对，不能任选一方覆盖另一方。', '仓库签收退件但是售后仍在途怎么办', '退货仓已确认收货而客服系统还显示运输中，应信哪个'),
    ('stock', 'warehouse', '库存盘点', '实盘差异', '模拟规则：同一SKU的预期库存与实盘数量不一致时记录库存差异，核对盘点时间、入库和出库流水。只读系统不得直接修改库存。', '预期库存和实盘数量不一致怎么办', '盘点少了两件，机器人能不能自动补账'),
    ('stalled', 'logistics', '物流监控', '轨迹停滞', '模拟规则：物流状态为stalled且24小时没有轨迹更新时标记停滞。核对承运商、最后轨迹时间与运单，未获得原因证据时不能声称丢件。', '物流停滞一天没有新轨迹如何排查', '包裹轨迹24小时没变，能认定丢失了吗'),
    ('proof', 'logistics', '签收核验', '签收争议', '模拟规则：客户称未收到但承运商显示签收时，应收集签收凭证、签收人及站点回执，保存争议证据并转人工。物流签收状态不自动证明客户本人收货。', '物流签收客户却说没有收到怎么办', '快递显示投递完成但买家否认收货需要什么材料'),
    ('compensation', 'aftersales', '晚到赔付', '申请材料', '模拟规则：晚到赔付申请需要订单承诺时间、实际送达时间和运单证据。\n| 材料 | 用途 |\n| 承诺时间 | 计算是否晚到 |\n| 送达回执 | 核对实际送达 |\n赔付金额由人工核实政策后决定，查询助手不执行打款。', '申请晚到赔付需要哪些材料', '运输晚于承诺送到，赔偿申请要准备什么证据'),
    ('damage', 'aftersales', '破损赔付', '申请材料', '模拟规则：破损赔付申请需要外包装照片、商品破损照片、运单和验收记录。\n| 材料 | 用途 |\n| 包装照片 | 核对外箱 |\n| 商品照片 | 核对破损部位 |\n赔付金额需人工审核，不自动承诺固定赔偿比例。', '商品破损赔付需要什么证据', '外箱和货物都坏了，理赔要提交哪些照片'),
    ('split', 'order', '订单履约', '拆单发货', '模拟规则：一个订单拆成多个包裹时分别核对每个子包裹和运单。只有全部子包裹均满足送达条件才可判断整单履约完成，不能用一个包裹签收代表全单。', '拆单发货一个包裹签收算整单完成吗', '同一订单分两票运输，其中一票已到能否判全部送达'),
    ('sku', 'warehouse', '商品标识', 'SKU匹配', '模拟规则：SKU-AX7与SKU-AX70是不同商品编码，检索和库存核对需精确匹配完整SKU。不得使用前缀命中替代完整商品身份。', 'SKU-AX7和SKU-AX70可以当同一种货吗', '商品编码只差末尾一个零，能合并盘点吗'),
    ('supplier', 'order', '采购协同', '供应商延期', '模拟规则：供应商承诺交期晚于采购需求日期时记录供货延期风险，核对采购单、承诺交期和当前到货记录。没有交期或到货证据时不得编造延期天数。', '供应商交期晚于采购需求如何处理', '供货商承诺送货日已超过需要日期，应核对哪些单据'),
    ('human', 'order', '人工介入', '停止自动处理', '模拟规则：出现缺失证据、相互冲突的状态、用户主动要求人工或高风险写操作时，应停止自动结论或操作并转人工。当前查询服务只读，不能执行退款、赔付或改库存。', '客户要求人工还应该继续自动退款吗', '证据不全或要改库存时助手应该如何处理'),
]


def main():
    folder = ROOT / 'data/knowledge'
    folder.mkdir(parents=True, exist_ok=True)
    docs, cases = [], []
    for slug, domain, title, heading, body, dev, holdout in RULES:
        doc = {'doc_id': 'SIM-KB-' + slug, 'tenant_id': 'demo-a', 'domain': domain,
               'title': title, 'revision': 'v1', 'valid_from': '2026-01-01', 'valid_to': None,
               'published': True, 'sections': [{'section_id': slug, 'heading': heading, 'body': body}]}
        docs.append(doc)
        for split, query in [('dev', dev), ('holdout', holdout)]:
            cases.append({'id': split + '-' + slug, 'split': split, 'query': query, 'gold_section': slug})
    # Hard negatives for authorization/effectivity: never eligible for demo-a at the fixed as_of.
    for name, changes in [('foreign', {'tenant_id': 'demo-b'}),
                          ('expired', {'valid_to': '2026-01-01', 'valid_from': '2025-01-01'}),
                          ('future', {'valid_from': '2027-01-01'}), ('draft', {'published': False})]:
        doc = dict(docs[0], doc_id='SIM-KB-' + name, **changes)
        doc['sections'] = [{'section_id': name, 'heading': '不可用于当前身份和时间',
                            'body': '退货入库48小时未退款的错误模拟政策：直接执行退款，不需要核实。'}]
        docs.append(doc)
    corpus = {'corpus_id': 'synthetic-policies-v1', 'simulated': True,
              'source': 'Independently authored synthetic policies; not supplied company manuals.',
              'as_of': '2026-10-28', 'documents': docs}
    encoded = json.dumps(corpus, ensure_ascii=False, indent=2).encode()
    (folder / 'corpus-v1.json').write_bytes(encoded)
    evaluation = {'dataset_id': 'synthetic-retrieval-qa-v1', 'corpus_sha256': hashlib.sha256(encoded).hexdigest(),
                  'split_note': '12 dev and 12 paraphrase holdout questions share policy topics; author-owned, not blind generalization.',
                  'cases': cases}
    (folder / 'qa-v1.json').write_text(json.dumps(evaluation, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Saved 16 synthetic documents and 24 labeled queries; no model calls.')


if __name__ == '__main__':
    main()
