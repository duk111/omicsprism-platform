"""Render the retained live captures without changing or scoring model output."""
import hashlib
import json
import math
from pathlib import Path
import statistics

ROOT = Path(__file__).parent
before = json.loads((ROOT / 'baseline.json').read_text(encoding='utf-8'))
after = json.loads((ROOT / 'after.json').read_text(encoding='utf-8'))
baseline_repeat = json.loads((ROOT / 'baseline-repeat.json').read_text(encoding='utf-8')) if (ROOT / 'baseline-repeat.json').exists() else None
after_repeat = json.loads((ROOT / 'after-repeat.json').read_text(encoding='utf-8')) if (ROOT / 'after-repeat.json').exists() else None
assert [c['question'] for c in before['cases']] == [c['question'] for c in after['cases']]
assert before['settings'] == after['settings']
lock = json.loads((ROOT / 'baseline-lock.json').read_text(encoding='utf-8'))
assert lock['baseline_sha256'] == hashlib.sha256((ROOT / 'baseline.json').read_bytes()).hexdigest()

NOTES = {
    'q01': ('直接解释两轴；概念基本正确，中文自然。', '仍直接回答，缩短为三句；“折叠变化”译法仍不够自然。'),
    'q02': ('模型原文解释正确，最终被结果域无证据保护替换为拒答。', '中文同义概念表达进入已有 QA 路径；直接解释校正 p 值与变化幅度，不再要求 Job。'),
    'q03': ('正确区分多重检验与效应量，英文自然。', '仍正确、英文自然；不将 adjusted p 误当效应量或零假设概率。'),
    'q04': ('明确相关不等于调控，中文自然，略长。', '主结论正确；后续列举功能注释等不能单独证实调控，应更准确说明实验证据。'),
    'q05': ('模型解释相关，但被 Analysis 缺输入文案覆盖。', '进入已有 QA 路径；直接解释正负与参考方向，不再要求上传。'),
    'q06': ('模型原文解释相关，最终要求 Job，过度拒答。', '直接解释 VIP 与 padj 并回答不能只看 VIP；但把校正 p 值称为“显著性水平”不够严谨，应与预设 alpha 区分，保留为科学措辞问题。'),
    'a01': ('DEG 相关，列出无关模块，GMA 调控推断措辞偏强。', '更短且不再声称调控推断；但用户已口述有文件，措辞“需要补充”混淆文件已拥有与尚未上传。'),
    'a02': ('所需输入存在，但列出三模块和整套后续流程，冗长。', '直接给出 DEG 的 counts + metadata，英文一致，保留执行前确认。'),
    'a03': ('识别 DEM，但机械列出三模块和流程。', '直接给出 DEM 的 metabolome + metadata，中文简短，保留确认。'),
    'a04': ('正确解释 tested/reference，未臆测实际组别。', '含义正确，但更长并重复索取信息；提示词未稳定改善。'),
    'r01': ('已查到证据，裸 JSON 格式错误及两次重试后英文失败。', '直接返回 gene_up 的精确 padj；中文自然，引用有效，调用减少。'),
    'r02': ('英文独立问题含 its，被前置追问规则阻断；0 次调用，中文澄清。', '修复同句指代误判后，经原查询和 Verifier 给出精确英文短句；3 次调用对应原来被跳过的真实查询。'),
    'r03': ('数值正确，整行重复且丢失统计解释。', '只展示两项精确统计值并附一般定义；不舍入或推断具体比较方向；引用有效。'),
    'r04': ('匹配结果正确，但多余字段且正文/证据重复。', '只展示 gene_up 与精确 padj，证据一次展示；结果保持一致。'),
    'r05': ('模型猜 result_view 路径后错误声称不存在；最终保护拒答。', '未解决；仍猜测 result_view 路径，失败不能作为不存在证据，最终安全拒答；需要进一步提升模型工具参数可靠性，不能算查无匹配验收通过。'),
    'r06': ('名称和数值正确，但整行机械展开与重复。', '简短中文名称+padj_bh，数值及引用保持精确。'),
    'r07': ('数值正确，但英文问题收到整行字段。', '自然英文，只含两项精确统计值，引用有效。'),
    'r08': ('PearsonR 正确，缺因果解释；双实体整行重复。', '保留双端点原始行，补充一般的相关不等于因果说明；不作具体机制断言。仍冗长。'),
    'r09': ('数值正确，但完整网络行展开及重复。', '数值与双端点保留、引用有效、重复消除；受现有 Verifier 语法限制，仍非自然英文短句。'),
    'r10': ('无当前焦点，工具可列举唯一隔离任务；最终空证据，未答目标值。', '列举并选择唯一可访问任务后成功返回真实 padj；不是新增上下文或权限能力。不能将此题当作无可访问 Job 的负例。'),
}


def metrics(report):
    cases = report['cases']
    times = sorted(c['latency_ms'] for c in cases)
    grounded = [c for c in cases if 'verification' in c]
    duplicates = 0
    for case in cases:
        texts = [b['text'] for b in case.get('blocks', []) if b['type'] == 'text']
        claims = [claim['text'] for b in case.get('blocks', []) if b['type'] == 'evidence' for claim in b['claims']]
        duplicates += bool(claims and any(text == '\n'.join(claims) for text in texts))
    return {
        'cases': len(cases),
        'model_http_calls': sum(len(c['model_calls']) for c in cases),
        'mean_latency_ms': round(statistics.mean(times), 2),
        'p50_latency_ms': round(statistics.median(times), 2),
        'p95_latency_ms_nearest_rank': times[math.ceil(len(times) * .95) - 1],
        'grounded_answers': len(grounded),
        'approved_grounded_answers': sum(c['verification']['verdict'] == 'approved' for c in grounded),
        'exact_text_evidence_duplicate_turns': duplicates,
        'graph_accounted_model_steps': sum(c.get('budget', {}).get('used_model_steps', 0) for c in cases),
    }


def visible_answer(case):
    """Approximate MessageBlocks' visible text for a reviewable comparison."""
    parts = []
    for block in case.get('blocks', []):
        if block.get('type') == 'text':
            parts.append(block.get('text', ''))
        elif block.get('type') == 'evidence':
            parts.extend(claim.get('text', '') for claim in block.get('claims', []))
    return '\n'.join(part for part in parts if part)


baseline_lines = ['# 20 问修改前实际回复与人工审阅', '', '原始模型、工具调用与返回证据：baseline.json。以下保留最终回复原文，未重写答案。', '']
comparison = ['# 同一 20 问：修改前后实际回复', '',
              '模型均为真实部署的 Qwen3-14B-AWQ；模型、问题、采样参数、隔离数据相同。每题单次采集，保留失败题。结果是本地现有生产链路调用部署模型，不是生产 API/队列/浏览器验收。', '',
              '“修改后”包含生产后端从真实查询结果确定性生成并通过原 Verifier 的短句与一般术语说明；不是模型原文的人工润色。原始模型回复完整保留于 after.json 的 model_calls。', '',
              '## 客观记录（不等同于科研回答质量评分）', '', '| 指标 | 修改前 | 修改后 |', '| --- | ---: | ---: |']
bm, am = metrics(before), metrics(after)
for key in bm:
    comparison.append(f'| {key} | {bm[key]} | {am[key]} |')
comparison += ['', 'p95 使用 nearest rank；无预热配对试验、无多次重复，单次延迟不能证明显著改善。approved 仅表示现有确定性 Verifier 接受，不能证明概念解释质量或因果结论正确。`used_model_steps` 包括现有确定性节点记账，不等同于 HTTP 调用数。', '']
if baseline_repeat and after_repeat:
    comparison += ['## 延迟复测', '',
                   '为区分服务波动与回归，使用同一 20 问再各运行一次；问题、模型、参数和隔离数据相同。', '',
                   '| 指标 | 修改前复测 | 修改后复测 |', '| --- | ---: | ---: |']
    for key in ('mean_latency_ms', 'p50_latency_ms', 'p95_latency_ms_nearest_rank', 'model_http_calls'):
        def stat(data, name):
            values = sorted(c['latency_ms'] for c in data['cases']) if name != 'model_http_calls' else None
            if name == 'mean_latency_ms': return round(statistics.mean(values), 2)
            if name == 'p50_latency_ms': return round(statistics.median(values), 2)
            if name == 'p95_latency_ms_nearest_rank': return values[math.ceil(len(values) * .95) - 1]
            return sum(len(c['model_calls']) for c in data['cases'])
        comparison.append(f'| {key} | {stat(baseline_repeat, key)} | {stat(after_repeat, key)} |')
    comparison += ['', '两次复测均保持 39→40 次 HTTP 调用（概念题路由修复使原来跳过的英文题进入真实查询）；修改后复测平均与 P95 均低于修改前复测。该证据支持没有明显延迟回归，但不替代长期监控。', '']
for b, a in zip(before['cases'], after['cases']):
    cid = b['id']
    baseline_lines += [f"## {cid}：{b['question']}", '', b.get('answer') or '(无回复)', '', '审阅：' + NOTES[cid][0], '',
                       f"模型 HTTP 调用：{len(b['model_calls'])}；耗时：{b['latency_ms']} ms；验证：{b.get('verification', {}).get('verdict', '无 grounded answer')}。", '']
    comparison += [f"## {cid}：{b['question']}", '', '**修改前（UI 可见内容）**', '', visible_answer(b) or '(无回复)', '', '**修改后（UI 可见内容）**', '', visible_answer(a) or '(无回复)', '',
                   '审阅：' + NOTES[cid][1], '', f"模型 HTTP 调用：{len(b['model_calls'])} → {len(a['model_calls'])}；耗时：{b['latency_ms']} → {a['latency_ms']} ms；修改后验证：{a.get('verification', {}).get('verdict', '无 grounded answer')}。", '']
(ROOT / 'baseline-answers.md').write_text('\n'.join(baseline_lines), encoding='utf-8')
(ROOT / 'comparison.md').write_text('\n'.join(comparison), encoding='utf-8')
(ROOT / 'metrics.json').write_text(json.dumps({'before': bm, 'after': am}, indent=2), encoding='utf-8')
print(json.dumps({'before': bm, 'after': am}, indent=2))
