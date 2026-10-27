"""Reply projection preserves scientific boundaries and works in both languages."""
import pytest

from backend.app.agent.grounding import GroundedAnswerPipeline, metric_explanation
from backend.app.agent.message_blocks import grounded_blocks
from backend.app.agent.schemas import AgentEvidenceBlock, AgentTextBlock
from backend.app.agent.verifier import AnswerVerifier, FALLBACK_TEXT
from backend.tests.test_agent_evidence_reliability import evidence, draft


@pytest.mark.parametrize('question,expected', [
    ('查询 GeneA 的 padj，只给出该值。', 'GeneA 的 padj 为 0.001。'),
    ('Show padj for GeneA only.', 'GeneA has padj 0.001.'),
])
def test_requested_values_are_verified_without_unrelated_fields(question, expected):
    source = evidence()
    answer = GroundedAnswerPipeline().answer(source, user_message=question)
    assert [claim.text for claim in answer.claims] == [expected]
    assert AnswerVerifier().verify(answer, [source]).verdict.value == 'approved'
    assert answer.claims[0].citation.checksum == source.checksum
    assert answer.claims[0].citation.row_ids == [1]
    blocks = grounded_blocks(answer)
    assert len(blocks) == 1 and isinstance(blocks[0], AgentEvidenceBlock)


def test_projection_does_not_silently_drop_nonfinite_or_unknown_requested_fields():
    source = evidence([{'_row_id': 1, 'gene_id': 'GeneA', 'padj': 'NaN'}])
    answer = GroundedAnswerPipeline().answer(source, user_message='解释 padj 和 missing_statistic')
    assert answer.claims[0].text == 'gene_id=GeneA; padj=NaN'
    assert AnswerVerifier().verify(answer, [source]).verdict.value == 'approved'


def test_bad_model_claim_still_falls_back_without_accepting_fabrication():
    source = evidence()
    answer = GroundedAnswerPipeline().answer(source, draft('GeneZ has padj 0.001'), user_message='查询 padj')
    assert answer.claims[0].text == FALLBACK_TEXT
    assert all('GeneZ' not in claim.text for claim in answer.claims)


def test_network_keeps_both_endpoints_and_only_adds_general_correlation_caveat():
    source = evidence([{'_row_id': 7, 'Gene': 'GeneA', 'Metabolite': 'MetA', 'PearsonR': '-0.9'}])
    question = '查询 GeneA 和 MetA 的 PearsonR，这能证明因果关系吗？'
    answer = GroundedAnswerPipeline().answer(source, user_message=question)
    assert 'Gene=GeneA' in answer.claims[0].text and 'Metabolite=MetA' in answer.claims[0].text
    assert AnswerVerifier().verify(answer, [source]).verdict.value == 'approved'
    note = metric_explanation(question, source)
    assert '不能证明因果关系' in note
    blocks = grounded_blocks(answer, note)
    assert isinstance(blocks[0], AgentEvidenceBlock) and isinstance(blocks[1], AgentTextBlock)
    assert '-0.9' not in note


def test_empty_evidence_never_gets_a_statistical_interpretation():
    source = evidence([])
    assert metric_explanation('解释 padj 和 log2FoldChange', source) == ''
    assert metric_explanation('Show padj only', evidence()) == ''


def test_explanations_do_not_invent_contrast_direction_or_effect_size():
    note = metric_explanation('解释 log2FoldChange 和 padj', evidence())
    assert 'tested/reference' in note and '不表示变化幅度' in note
    assert 'salt' not in note and '显著上调' not in note and '2.3' not in note


@pytest.mark.parametrize('question', [
    'DEG 中 padj 是什么？padj 小是否意味着变化幅度大？',
    'log2FoldChange 的正负如何解释？需要知道什么比较方向？',
    '代谢组分析中 VIP 和 padj 分别说明什么？能只看 VIP 吗？',
])
def test_general_chinese_definitions_use_existing_qa_route(question):
    from backend.app.agent.graph import AgentRole, GraphState
    from backend.app.agent.router import route
    state = GraphState(thread_id='definition', user_id='user', user_message=question)
    assert route(state) is AgentRole.QA
    # An explicit request about current result data still needs evidence.
    state.user_message = '查询当前结果中 gene_up 的 padj，' + question
    assert route(state) is AgentRole.RESULT_QA


def test_self_contained_question_does_not_reuse_selection_but_subset_still_does():
    from backend.app.agent.result_context import is_result_followup
    assert not is_result_followup('Which gene has the smallest padj in the current DEG results? Give its name and padj only.')
    assert is_result_followup('Which gene among them has the largest log2FoldChange? Give its name.')
    assert is_result_followup('What is its padj?')
