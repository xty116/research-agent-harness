import json
from pathlib import Path

from fastapi.testclient import TestClient

from research_agent.domain import ClaimAssessment, Document, ReportDraft, RunState
from research_agent.report_model import channel_summary, safe_url
from research_agent.reporting import backfill_reports, safe_link, validate_report
from research_agent.web import create_app


def record(store, run_id, status='partial', mode='demo', day='2026-11-01', headline='Regression report'):
    state = RunState(run_id=run_id, local_day=day, mode=mode, prompt='Daily research',
                     draft=ReportDraft(headline=headline))
    store.create_run(state)
    if status != 'running':
        store.finish(state, status)
    return state


def test_catalog_paginates_every_record_and_filters_before_limit(store):
    for i in range(107):
        record(store, f'run_{i:03}', day='2026-10-31' if i % 2 else '2026-11-01')
    record(store, 'eval_one', 'evaluated', mode='live', headline='Unique evaluation')
    result = store.report_catalog(page=9)
    assert result['total'] == 108 and len(result['items']) == 12
    assert len({r['id'] for p in range(1, 10) for r in store.report_catalog(page=p)['items']}) == 108
    assert store.report_catalog(kind='evaluation')['items'][0]['id'] == 'eval_one'
    assert store.report_catalog(status='evaluated', query='Unique')['total'] == 1
    assert store.report_catalog(start='2026-11-01', end='2026-11-01')['total'] == 55
    assert store.report_catalog(query='run_%')['total'] == 0  # literal wildcard characters
    assert store.report_catalog(page=100)['page'] == 9


def test_backfill_preserves_every_terminal_status_checkpoint_and_usage(settings, store):
    for run_id, status in [('partial', 'partial'), ('cancelled', 'interrupted'), ('eval_one', 'evaluated'),
                           ('source-health-one', 'partial'), ('eval_stopped', 'evaluation_interrupted')]:
        record(store, run_id, status)
    record(store, 'active', 'running')
    original = {r['id']: r for r in store.all_runs()}
    usage = store.usage(mode='demo')
    result = backfill_reports(settings, store)
    assert result == {'generated': 5, 'skipped': 1, 'failed': []}
    for row in store.all_runs():
        before = original[row['id']]
        assert all(row[k] == before[k] for k in ('status', 'checkpoint', 'updated_at', 'error'))
        if row['id'] != 'active':
            path = Path(row['report_path'])
            assert all(path.with_suffix(s).is_file() for s in ('.md', '.json', '.report.json', '.html'))
            assert json.loads(path.with_suffix('.report.json').read_text())['status'] == row['status']
    assert store.usage(mode='demo') == usage
    assert backfill_reports(settings, store) == {'generated': 0, 'skipped': 6, 'failed': []}


async def test_all_report_routes_exports_tabs_and_mode_isolation(harness, settings):
    state = await harness.run()
    with TestClient(create_app(settings, harness, schedule=False)) as client:
        for path in ('/', '/reports', '/evidence', '/evidence?tab=questions', '/skills', '/about'):
            response = client.get(path)
            assert response.status_code == 200
            assert 'built-in method' not in response.text
        for tab in ('report', 'evidence', 'trace', 'tasks'):
            assert client.get(f'/runs/{state.run_id}?tab={tab}').status_code == 200
        for extension in ('md', 'json', 'html', 'state.json'):
            response = client.get(f'/runs/{state.run_id}/export.{extension}')
            assert response.status_code == 200
            assert 'attachment;' in response.headers['content-disposition']
            if extension == 'json':
                assert response.json()['schema_version'] == '1.0'
            if extension == 'html':
                assert '<style>' in response.text and '/static/' not in response.text
                assert '<script' not in response.text
        assert client.get(f'/runs/{state.run_id}?mode=live').status_code == 404
        assert client.get('/api/report-catalog?mode=live').json()['total'] == 0
        assert client.post(f'/runs/{state.run_id}').status_code == 405
        assert 'text/html' in client.get('/missing').headers['content-type']
        assert client.get('/reports?start=not-a-date').status_code == 422
        assert client.get('/reports?start=2026-11-02&end=2026-11-01').status_code == 200


def test_event_cursor_does_not_drop_or_repeat_events(harness, settings, store):
    state = record(store, 'long_trace')
    for i in range(205):
        store.event(state.run_id, 'observation', {'summary': f'Observation {i}'})
    with TestClient(create_app(settings, harness, schedule=False)) as client:
        first = client.get(f'/api/runs/{state.run_id}/events?limit=100').json()
        second = client.get(f'/api/runs/{state.run_id}/events?limit=100&after={first[-1]["seq"]}').json()
        third = client.get(f'/api/runs/{state.run_id}/events?limit=100&after={second[-1]["seq"]}').json()
        assert len(first + second + third) == len({e['seq'] for e in first + second + third}) == 205
        assert 'Observation 100' in client.get(f'/runs/{state.run_id}?tab=trace&after={first[-1]["seq"]}').text


def test_external_content_is_escaped_in_html_and_markdown(harness, settings, store):
    state = record(store, 'unsafe', headline='<script>alert("headline")</script>')
    document = store.save_document(state.run_id, Document(url='javascript:alert(1)', title='<img src=x onerror=alert(1)>',
                                   text='<script>alert("source")</script>', source='test', kind='full_text'))
    state.evidence_ids = [document.id]
    store.finish(state, 'partial')
    with TestClient(create_app(settings, harness, schedule=False)) as client:
        for path in (f'/runs/{state.run_id}', f'/runs/{state.run_id}/export.html', f'/sources/{document.id}'):
            response = client.get(path)
            assert response.status_code == 200
            assert '<script>alert' not in response.text and '<img src=x' not in response.text
            assert 'href="javascript:' not in response.text
        markdown = client.get(f'/runs/{state.run_id}/export.md').text
        assert '<script>' not in markdown and '&lt;script&gt;' in markdown
    assert safe_url('https://user:password@example.com/') == ''
    assert safe_url('https://example.com/\n') == ''
    assert safe_link('https://example.com/a) <script>') == 'https://example.com/a%29%20%3Cscript%3E'


def test_source_page_keeps_immutable_versions(harness, settings, store):
    state = record(store, 'versions')
    original = store.save_document(state.run_id, Document(url='https://example.com/same', title='Evidence', text='Original version', source='test'))
    revised = store.save_document(state.run_id, original.model_copy(update={'text': 'Revised version'}))
    assert original.id != revised.id
    with TestClient(create_app(settings, harness, schedule=False)) as client:
        assert 'Original version' in client.get(f'/sources/{original.id}').text
        assert 'Revised version' in client.get(f'/sources/{revised.id}').text


def test_partial_channel_never_looks_fully_covered():
    coverage = {'web:benchmark': {'status': 'ok', 'items': 2}, 'papers': {'status': 'partial', 'items': 1}}
    channels = channel_summary(coverage)
    assert channels[0]['status'] == 'partial'
    assert channels[1]['status'] == 'partial'
    assert channels[-1]['status'] == 'not_scanned'


def test_recommendation_cannot_use_superseded_evidence(store):
    state = record(store, 'superseded')
    claim = ClaimAssessment(claim='Model A ranks first', verdict='support', rationale='Initial evidence')
    old = store.save_claim(state.run_id, 'task', claim)
    new = store.save_claim(state.run_id, 'task', claim.model_copy(update={'verdict': 'unverified', 'rationale': 'Version missing'}))
    state.claim_ids = [old, new]
    draft = ReportDraft(headline='Revised evidence', recommendations=[{'priority': 'P0', 'action': 'Acquire dataset', 'basis_claim_ids': [old]}])
    assert 'Recommendation uses a superseded claim revision' in validate_report(draft, state, store)
