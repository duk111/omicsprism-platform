"""PR 8: actual core exports plus adversarial query/evidence regressions."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

import pytest

from backend.app.agent.grounding import EvidenceGroundingError, GroundedAnswerPipeline
from backend.app.agent.schemas import Citation, GroundedAnswer, GroundedClaim, ToolName
from backend.app.agent.verifier import AnswerVerifier
from backend.app.models import AnalysisType
from backend.tests.test_agent_artifact_e2e import invoke, persisted_runtime
from backend.tests.test_agent_result_tools import _artifact, _job, _runtime

FIXTURES = Path(__file__).parents[1] / 'app/agent/fixtures/result_query'


def runtime_for(text, kind=AnalysisType.DEG, name='salt.all.csv'):
    artifact = _artifact().model_copy(update={'filename': name, 'path': 'outputs/' + name})
    job = _job(artifact=artifact).model_copy(update={'analysis_type': kind})
    return _runtime(job, text), artifact.path


@pytest.mark.parametrize('method', ['query_result', 'query_artifact'])
@pytest.mark.parametrize('kind,field,entity', [
    (AnalysisType.DEG, 'gene_id', 'GeneA'),
    (AnalysisType.DEM, 'metabolite_id', 'MetA'),
])
def test_resolve_entity_uses_analysis_and_observed_header(method, kind, field, entity):
    runtime, path = runtime_for(f'{field},log2FoldChange,padj\nOther,0,0.1\n{entity},2,0.001\n', kind)
    result = getattr(runtime, method)('job-1', path, resolve_entity=entity)
    assert result.ok and result.row_count == 1
    assert result.rows[0][field] == entity and result.rows[0]['_row_id'] == 2
    assert result.filters == {field: entity}


@pytest.mark.parametrize('field,alias,entity', [('Gene', 'gene_id', 'GeneA'), ('Metabolite', 'metabolite_id', 'MetA')])
def test_gma_explicit_entity_and_alias(field, alias, entity):
    runtime, path = runtime_for('Gene,Metabolite,PearsonR\nGeneB,MetB,0.2\nGeneA,MetA,-0.9\n',
                                AnalysisType.GMA, 'T02_High_Confidence_Network.csv')
    for key in (field, alias):
        result = runtime.query_artifact('job-1', path, filters={key: entity})
        assert result.ok and result.rows[0][field] == entity
        assert result.rows[0]['_row_id'] == 2 and result.filters == {field: entity}
    ambiguous = runtime.query_result('job-1', path, resolve_entity=entity)
    assert not ambiguous.ok and ambiguous.error_code == 'ambiguous_entity_field'


def test_gma_single_metabolite_column_resolves_without_gene_assumption():
    runtime, path = runtime_for('Module,Metabolite,SpearmanRho,FDR\nM1,MetA,-0.8,0.01\n',
                                AnalysisType.GMA, 'T05_Module_Metabolite_Association.csv')
    assert runtime.query_result('job-1', path, resolve_entity='MetA').rows[0]['Metabolite'] == 'MetA'


def test_no_match_is_successful_empty_evidence_with_identity():
    runtime, path = runtime_for('gene_id,padj\nGeneA,0.001\n')
    result = runtime.query_result('job-1', path, resolve_entity='Absent')
    assert result.ok and result.error_code == 'no_match'
    assert result.rows == [] and result.row_count == 0 and not result.truncated
    assert result.artifact == path and result.checksum == 'sha256:fixture'
    answer = GroundedAnswerPipeline().answer(result)
    assert AnswerVerifier().verify(answer, [result]).verdict.value == 'approved'


@pytest.mark.parametrize('text', ['gene_id,padj\nGeneA,0.1\n', 'gene_id,padj\n'])
@pytest.mark.parametrize('kwargs', [{'filters': {'absent': ''}}, {'sort': 'absent asc'}])
def test_missing_fields_checked_against_header_even_when_empty(text, kwargs):
    runtime, path = runtime_for(text)
    result = runtime.query_artifact('job-1', path, **kwargs)
    assert not result.ok and result.error_code == 'field_not_found'
    assert result.artifact == path


@pytest.mark.parametrize('direction,ids', [('asc', [7, 4, 8, 6, 5]), ('desc', [5, 6, 4, 8, 7])])
def test_numeric_sort_is_finite_stable_and_before_limit(direction, ids):
    text = 'gene_id,padj\nA,NaN\nB,\nC,Inf\nD,0.001\nE,0.1\nF,0.02\nG,0\nH,1e-3\nI,-Infinity\nJ,NA\n'
    runtime, path = runtime_for(text)
    result = runtime.query_artifact('job-1', path, sort=f'padj {direction}', limit=5)
    assert [r['_row_id'] for r in result.rows] == ids
    assert result.row_count == 10 and result.truncated
    full = runtime.query_result('job-1', path, sort=f'padj {direction}')
    assert [r['_row_id'] for r in full.rows[5:]] == [1, 2, 3, 9, 10]


@pytest.mark.parametrize('filters,ids', [
    ({'padj__lt': 0.05}, [1, 3, 5]),
    ({'log2FoldChange__gt': 1}, [1, 4]),
    ({'log2FoldChange__gte': 1}, [1, 2, 4]),
    ({'log2FoldChange__lte': 0}, [3, 5]),
    ({'log2FoldChange__eq': 0}, [3]),
    ({'log2FoldChange__ne': 0}, [1, 2, 4, 5]),
    ({'log2FoldChange': 0}, [3]),
    ({'padj__lt': 0.05, 'log2FoldChange__gt': 1}, [1]),
])
def test_finite_numeric_comparisons(filters, ids):
    runtime, path = runtime_for('gene_id,padj,log2FoldChange\nA,0.001,2\nB,0.1,1\nC,0,0.0\nD,NaN,10\nE,0.02,-2\nF,,\nG,Inf,NaN\nH,-Inf,Inf\n')
    result = runtime.query_artifact('job-1', path, filters=filters)
    assert result.ok and [r['_row_id'] for r in result.rows] == ids


def test_correlation_absolute_threshold_preserves_original_signed_cells():
    runtime, path = runtime_for('Gene,Metabolite,PearsonR,EdgeWeight\nGeneA,MetA,-0.9,0.8\nGeneA,MetB,0.7,0.7\nGeneB,MetC,NaN,0\n',
                                AnalysisType.GMA, 'T02_High_Confidence_Network.csv')
    result = runtime.query_artifact('job-1', path, filters={'PearsonR__abs_gt': 0.8})
    assert result.ok and result.rows[0]['PearsonR'] == '-0.9' and result.row_count == 1


@pytest.mark.parametrize('kwargs,code', [
    ({'filters': {'padj__contains': '0'}}, 'invalid_filter'),
    ({'filters': {'padj__lt': 'nan'}}, 'invalid_filter'),
    ({'filters': {'padj__gt': True}}, 'invalid_filter'),
    ({'sort': 'padj sideways'}, 'invalid_sort'),
    ({'sort': '_row_id desc'}, 'field_not_found'),
])
def test_invalid_query_never_falls_back_to_string_matching(kwargs, code):
    runtime, path = runtime_for('gene_id,padj\nGeneA,0.001\n')
    result = runtime.query_artifact('job-1', path, **kwargs)
    assert not result.ok and result.error_code == code


def test_dem_adjusted_p_alias_preserves_actual_schema_and_values():
    runtime, path = runtime_for('metabolite_id,padj_bh\nMetA,0.02\nMetB,0.001\n', AnalysisType.DEM)
    result = runtime.query_result('job-1', path, filters={'padj__lt': 0.05}, sort='padj asc')
    assert result.ok and result.rows[0]['metabolite_id'] == 'MetB'
    assert 'padj' not in result.rows[0] and result.sort == 'padj_bh asc'
    assert result.filters == {'padj_bh__lt': 0.05}


@pytest.mark.parametrize('kind,name,field', [
    (AnalysisType.DEG, 'deg.all.csv', 'gene_id'),
    (AnalysisType.DEM, 'dem.all.csv', 'metabolite_id'),
    (AnalysisType.GMA, 'T02_High_Confidence_Network.csv', 'Gene'),
    (AnalysisType.GMA, 'T02_High_Confidence_Network.csv', 'Metabolite'),
])
def test_real_core_export_fixture_query_checksum_and_citations(tmp_path, kind, name, field):
    content = (FIXTURES / name).read_bytes()
    manifest = json.loads((FIXTURES / 'manifest.json').read_text())
    assert hashlib.sha256(content.replace(b'\r\n', b'\n')).hexdigest() == manifest[name]['fixture_sha256']
    rows = list(csv.DictReader(io.StringIO(content.decode('utf-8'))))
    runtime, job = persisted_runtime(tmp_path, kind, content, name)
    entity = rows[-1][field]
    source = runtime.query_result(job.id, 'outputs/' + name, filters={field: entity})
    expected = [dict(_row_id=i, **row) for i, row in enumerate(rows, 1) if row[field] == entity]
    assert source.ok and source.rows == expected
    assert source.checksum == hashlib.sha256(content).hexdigest()
    answer = GroundedAnswerPipeline().answer(source)
    assert [c.citation.row_ids[0] for c in answer.claims] == [r['_row_id'] for r in expected]
    assert AnswerVerifier().verify(answer, [source]).verdict.value == 'approved'


def test_sort_citation_and_entity_binding_survive_numeric_query(tmp_path):
    content = b'gene_id,padj,log2FoldChange\nGeneB,0.02,2\nGeneA,0.001,3\nGeneC,0.1,-2\n'
    runtime, job = persisted_runtime(tmp_path, AnalysisType.DEG, content, 'salt.all.csv')
    source = runtime.query_result(job.id, 'outputs/salt.all.csv', filters={'log2FoldChange__gt': 1}, sort='padj asc', limit=1)
    assert source.rows[0]['_row_id'] == 2
    citation = Citation(artifact=source.artifact, checksum=source.checksum, row_ids=[2])
    valid = GroundedAnswer(claims=[GroundedClaim(text='GeneA has padj 0.001', citation=citation)])
    assert GroundedAnswerPipeline().answer(source, valid) == valid
    for text, ids in [('GeneB has padj 0.001', [2]), ('GeneA has padj 0.001', [1])]:
        bad = GroundedAnswer(claims=[GroundedClaim(text=text, citation=citation.model_copy(update={'row_ids': ids}))])
        assert AnswerVerifier().verify(bad, [source]).verdict.value == 'rejected'


def test_changed_artifact_bytes_do_not_keep_old_checksum(tmp_path):
    runtime, job = persisted_runtime(tmp_path, AnalysisType.DEG, b'gene_id,padj\nGeneA,0.001\n', 'salt.all.csv')
    runtime.files.read_artifact_text = lambda *a, **k: 'gene_id,padj\nGeneZ,0.001\n'
    result = runtime.query_result(job.id, 'outputs/salt.all.csv')
    assert not result.ok and result.error_code == 'checksum_mismatch' and not result.rows


@pytest.mark.parametrize('action', ['tool_call', 'query_result'])
@pytest.mark.parametrize('kind,question,query,expected_ids', [
    (AnalysisType.DEG, '当前 DEG 结果中 padj 最小的 5 个基因是什么？',
     {'sort': 'padj asc', 'limit': 5}, [3, 6, 2, 5, 1]),
    (AnalysisType.DEM, '查询代谢物 MetA 的差异分析结果。',
     {'resolve_entity': 'MetA'}, [2]),
    (AnalysisType.GMA, '这个 GMA 结果中 GeneA 关联了哪些代谢物？',
     {'filters': {'Gene': 'GeneA'}, 'sort': 'EdgeWeight desc'}, [3, 1]),
])
def test_agent_questions_query_actual_rows_through_graph(tmp_path, action, kind, question, query, expected_ids):
    # Deliberate synthetic values use the complete observed core schema. Separate
    # tests above exercise verbatim Worker exports with their original IDs.
    if kind is AnalysisType.DEG:
        content = ('gene_id,baseMean,log2FoldChange,lfcSE,stat,pvalue,padj,comparison,volcano_status\n'
                   + ''.join(f'Gene{i},100,2,0.1,20,0.001,{p},salt,Up\n'
                             for i, p in enumerate(['0.1', '0.02', '0', 'NaN', '0.05', '0.001'], 1))).encode()
        name = 'salt.all.csv'
    elif kind is AnalysisType.DEM:
        content = (b'metabolite_id,tested_mean,reference_mean,fold_change,log2FoldChange,t_stat,pvalue,vip,comparison,tested_level,reference_level,n_tested,n_reference,padj_bh,dem_status\n'
                   b'MetB,2,1,2,1,4,0.1,1.1,salt,salt,control,3,3,0.1,Not Sig\n'
                   b'MetA,4,1,4,2,6,0.001,2,salt,salt,control,3,3,0.002,Up\n')
        name = 'salt.all.csv'
    else:
        content = (b'Source,Target,Interaction,Gene,Metabolite,PearsonR,PearsonFDR,SpearmanRho,SpearmanFDR,MIScore,ScreenSupportCount,ModelSupportCount,RRARank,EdgeWeight,Sign,EdgeTier\n'
                   b'GeneA,MetA,association,GeneA,MetA,-0.9,0.001,-0.9,0.001,0.8,3,2,2,0.8,negative,high_confidence\n'
                   b'GeneZ,MetZ,association,GeneZ,MetZ,0.8,0.01,0.8,0.01,0.5,2,2,3,0.7,positive,high_confidence\n'
                   b'GeneA,MetB,association,GeneA,MetB,0.95,0.001,0.95,0.001,0.9,3,2,1,0.9,positive,high_confidence\n')
        name = 'T02_High_Confidence_Network.csv'
    runtime, job = persisted_runtime(tmp_path, kind, content, name)
    path = 'outputs/' + name
    args = dict(artifact=path, **query)
    decision = ({'action': action, 'tool': 'query_artifact', 'arguments': dict(job_id=job.id, **args)}
                if action == 'tool_call' else {'action': action, 'result_query': args})
    result = invoke(runtime, job, {'decision': decision},
                    {'decision': {'action': 'answer'}, 'answer': 'GeneZ has padj 0.001'}, user_message=question)
    assert result.grounded_answer is not None
    claims = result.grounded_answer.claims
    assert [c.citation.row_ids[0] for c in claims] == expected_ids
    original = list(csv.DictReader(io.StringIO(content.decode())))
    source = runtime.query_result(job.id, path, **query)
    assert AnswerVerifier().verify(result.grounded_answer, [source]).verdict.value == 'approved'
    for claim in claims:
        row = original[claim.citation.row_ids[0] - 1]
        assert all(f'{key}={value}' in claim.text for key, value in row.items())
        assert claim.citation.checksum.removeprefix('sha256:') == hashlib.sha256(content).hexdigest()
    assert 'GeneZ' not in result.response_text


def test_alias_conflicts_and_entity_conflicts_do_not_silently_override():
    runtime, path = runtime_for('gene_id,padj\nGeneA,0.1\n')
    for kwargs in ({'filters': {'Gene': 'GeneB', 'gene_id': 'GeneA'}},
                   {'filters': {'gene_id': 'GeneA'}, 'resolve_entity': 'GeneB'}):
        result = runtime.query_artifact('job-1', path, **kwargs)
        assert not result.ok and result.error_code == 'invalid_filter'


def test_legacy_gene_header_and_numeric_entity_id_are_not_rewritten():
    runtime, path = runtime_for('Gene,padj\n001,0.1\n1,0.02\n')
    result = runtime.query_result('job-1', path, resolve_entity='001')
    assert result.ok and result.rows == [{'_row_id': 1, 'Gene': '001', 'padj': '0.1'}]


@pytest.mark.parametrize('text', ['gene_id,_row_id,padj\nGeneA,999,0.1\n', 'gene_id,padj,padj\nGeneA,0.1,0.2\n'])
def test_reserved_or_duplicate_headers_cannot_forge_citation_rows(text):
    runtime, path = runtime_for(text)
    result = runtime.query_result('job-1', path)
    assert not result.ok and result.error_code == 'invalid_schema'


def test_result_limit_applies_after_complete_numeric_sort():
    runtime, path = runtime_for('gene_id,padj\n' + ''.join(f'Gene{i},{100-i}\n' for i in range(100)))
    result = runtime.query_artifact('job-1', path, sort='padj asc', limit=50)
    assert len(result.rows) == 50 and result.row_count == 100 and result.truncated
    assert [r['_row_id'] for r in result.rows] == list(range(100, 50, -1))


def test_large_scalar_cannot_turn_byte_truncation_into_no_match():
    runtime, path = runtime_for('gene_id,detail\nGeneA,' + 'x' * 40000 + '\n')
    source = runtime.query_result('job-1', path)
    assert not source.ok and source.error_code == 'result_exceeds_response_limit'
    assert source.row_count == 1 and source.rows == [] and source.truncated
    with pytest.raises(EvidenceGroundingError):
        GroundedAnswerPipeline().answer(source)


@pytest.mark.parametrize('prefix', ['', 'sha256:'])
def test_real_checksum_spelling_crlf_bom_and_multiline_records_are_preserved(tmp_path, prefix):
    content = ('\ufeffgene_id,padj,detail\r\nGeneB,0.02,plain\r\n'
               'GeneA,0.001,"first line\r\nsecond line"\r\n').encode('utf-8')
    runtime, job = persisted_runtime(tmp_path, AnalysisType.DEG, content, 'salt.all.csv')
    job.artifacts[0].checksum = prefix + hashlib.sha256(content).hexdigest()
    runtime.job_store.save(job)
    source = runtime.query_result(job.id, 'outputs/salt.all.csv', sort='padj asc', limit=1)
    assert source.ok and source.rows[0]['_row_id'] == 2
    assert source.rows[0]['detail'] == 'first line\r\nsecond line'
    assert source.checksum == prefix + hashlib.sha256(content).hexdigest()


@pytest.mark.parametrize('kwargs', [{'resolve_entity': 'GeneA'}, {'filters': {'Gene': 'GeneA'}}])
def test_aggregate_table_has_no_gene_entity_column(kwargs):
    runtime, path = runtime_for('comparison,up_count,total_genes\nsalt,1,10\n', name='differential_gene_counts.csv')
    source = runtime.query_result('job-1', path, **kwargs)
    assert not source.ok and source.error_code in {'entity_field_not_found', 'field_not_found'}


def test_large_artifact_still_exposes_actual_columns_for_query_selection(tmp_path):
    content = b'gene_id,padj\n' + b'GeneA,0.001\n' * 25000
    runtime, job = persisted_runtime(tmp_path, AnalysisType.DEG, content, 'salt.all.csv')
    described = runtime.describe_artifacts(job.id)
    artifact = next(item for item in described.artifacts if item.path == 'outputs/salt.all.csv')
    assert artifact.schema.columns == ['gene_id', 'padj']


def test_missing_field_in_graph_is_not_empty_evidence_or_old_entity_claim(tmp_path):
    runtime, job = persisted_runtime(tmp_path, AnalysisType.DEG, b'gene_id,padj\nGeneA,0.001\n', 'salt.all.csv')
    result = invoke(runtime, job, {'decision': {'action': 'tool_call', 'tool': 'query_artifact', 'arguments': {
        'job_id': job.id, 'artifact': 'outputs/salt.all.csv', 'filters': {'missing_field': ''}}}},
        {'decision': {'action': 'answer'}, 'answer': 'GeneZ has padj 0.001'})
    assert result.grounded_answer is None and 'GeneZ' not in result.response_text
    assert 'field_not_found' in result.tool_observations[-1].summary


def test_decimal_sort_and_threshold_do_not_collapse_distinct_values():
    runtime, path = runtime_for('gene_id,padj\nGeneA,0.100000000000000002\nGeneB,0.100000000000000001\n')
    source = runtime.query_result('job-1', path, sort='padj asc',
                                  filters={'padj__lt': '0.100000000000000002'})
    assert source.ok and [row['gene_id'] for row in source.rows] == ['GeneB']


def test_one_evidence_row_must_fit_existing_grounded_claim_contract():
    runtime, path = runtime_for('gene_id,detail\nGeneA,' + 'x' * 1200 + '\n')
    source = runtime.query_result('job-1', path)
    assert not source.ok and source.error_code == 'result_exceeds_response_limit'
    assert not source.rows and source.row_count == 1 and source.truncated


def test_entity_id_numeric_inputs_still_use_exact_id_equality():
    runtime, path = runtime_for('gene_id,padj\n001,0.01\n1,0.02\n')
    source = runtime.query_result('job-1', path, filters={'gene_id': 1})
    assert source.ok and [row['gene_id'] for row in source.rows] == ['1']


@pytest.mark.parametrize('operation', ['abs_gt', 'abs_gte', 'abs_lt', 'abs_lte'])
def test_absolute_comparisons_use_finite_values_and_do_not_rewrite_signed_cells(operation):
    runtime, path = runtime_for('Gene,Metabolite,SpearmanRho\nGeneA,MetA,-0.8\nGeneA,MetB,0.9\nGeneA,MetC,NaN\n',
                                AnalysisType.GMA, 'T02_High_Confidence_Network.csv')
    source = runtime.query_result('job-1', path, filters={'SpearmanRho__' + operation: 0.8})
    ids = {'abs_gt': [2], 'abs_gte': [1, 2], 'abs_lt': [], 'abs_lte': [1]}
    assert source.ok and [row['_row_id'] for row in source.rows] == ids[operation]


def test_gma_explicit_entity_and_resolve_entity_must_agree():
    runtime, path = runtime_for('Gene,Metabolite,PearsonR\nGeneA,MetA,-0.9\nGeneB,MetB,0.8\n',
                                AnalysisType.GMA, 'T02_High_Confidence_Network.csv')
    source = runtime.query_result('job-1', path, filters={'Metabolite': 'MetA'}, resolve_entity='MetA')
    assert source.ok and source.rows[0]['Gene'] == 'GeneA'
