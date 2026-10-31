from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import tempfile
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Literal
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .application import create_harness
from .domain import RunState
from .rendering import ASSETS, render, standalone_report
from .report_model import channel_summary, report_bundle
from .reporting import fallback_draft, write_report
from .scheduler import scheduler_loop
from .settings import Settings, load_settings
from .store import Store
from .tools import tool_catalog

Mode = Literal['live', 'demo']
SKILL_LABELS = {
    'research-planning': ('研究规划', '结合业务价值、新线索、证据缺口与剩余资源，决定继续、换路径、完成或延期。'),
    'web-research': ('多通道取证', '从全网、论文和代码生态发现来源，读取原文，必要时使用受控浏览器。'),
    'benchmark-verification': ('评测口径核验', '对齐模型与评测版本、指标、分组及日期，区分全榜结果和子榜结果。'),
    'evidence-review': ('原子断言审查', '以原文引文评估具体断言，区分证据支持、冲突和未证实。'),
    'memory-recall': ('历史与待办召回', '按实体和问题召回带日期的历史记录，对时间敏感结论重新取证。'),
    'ecosystem-research': ('产业生态研究', '从身份匹配的原始来源研究竞品、公开团队变化与供应商信息。'),
    'dataset-profiling': ('数据样本初查', '检查有界本地样本的字段、缺失、重复与跨 split 重叠，只输出汇总。'),
}
EVENT_LABELS = {'started': '运行开始', 'source_scan': '通道扫描', 'triage': '线索筛选', 'plan': '研究计划',
    'decision': '模型决策', 'observation': '工具观察', 'context': '上下文整理', 'model_usage': '模型用量',
    'model_error': '模型调用异常', 'model_schema_error': '输出校验拦截', 'priority_changed': '优先级调整',
    'task_added': '新增研究问题', 'task_archived': '管理性归档', 'report_draft': '报告草稿',
    'finished': '运行结束', 'resumed': '恢复执行', 'error': '执行异常', 'evaluation_case': '标注案例',
    'acceptance_setup': '验收范围设定'}


def create_app(settings: Settings | None = None, harness=None, schedule: bool = True) -> FastAPI:
    settings = settings or load_settings()
    companions: dict[str, Store] = {}

    @asynccontextmanager
    async def lifespan(app):
        app.state.harness = harness or create_harness(settings)
        worker = asyncio.create_task(scheduler_loop(app.state.harness)) if schedule else None
        try:
            yield
        finally:
            if worker:
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker
            for store in companions.values():
                store.close()
            if harness is None:
                await app.state.harness.model.close()
                app.state.harness.store.close()

    app = FastAPI(title='Research · 数据情报工作台', version='0.2.0', lifespan=lifespan,
                  docs_url='/api/docs', redoc_url=None)
    hosts = ['127.0.0.1', 'localhost', 'testserver', '[::1]']
    if settings.web.host not in {'0.0.0.0', '::'}:
        hosts.append(settings.web.host)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    app.mount('/static', StaticFiles(directory=ASSETS / 'static'), name='static')

    @app.middleware('http')
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.update({'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
                                 'X-Frame-Options': 'DENY', 'Cache-Control': 'no-store'})
        if not request.url.path.endswith('/export.html'):
            response.headers['Content-Security-Policy'] = ("default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "script-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; "
                "frame-ancestors 'none'; form-action 'self'")
        return response

    def get_store(mode=None):
        selected = mode or settings.mode
        if selected not in {'live', 'demo'}:
            raise HTTPException(422, '数据模式无效')
        if selected == settings.mode:
            return app.state.harness.store
        if selected not in companions:
            companions[selected] = Store(settings.path(settings.data_dir) / f'agent-{selected}.sqlite3', settings.schedule.timezone)
        return companions[selected]

    def get_row(run_id, mode=None):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', run_id):
            raise HTTPException(404, '找不到这份记录')
        store = get_store(mode)
        row = store.get_run(run_id)
        if not row:
            raise HTTPException(404, '找不到这份记录')
        return store, row

    def bundle_from_row(store, row):
        state = RunState.model_validate_json(row['checkpoint'])
        return report_bundle(state, store, row['status'], state.draft or fallback_draft(state, store), row)

    def common(request, mode, title, active):
        selected = mode or settings.mode
        path = request.url.path if request.url.path in {'/', '/reports', '/evidence', '/skills', '/about'} else '/reports'
        return {'title': title, 'active': active, 'mode': selected,
                'counts': get_store(selected).overview_counts(), 'timezone': settings.schedule.timezone,
                'schedule': settings.schedule, 'mode_links': {m: f'{path}?mode={m}' for m in ('live', 'demo')}}

    def paginate_urls(request, result):
        params = dict(request.query_params)
        def link(page):
            return request.url.path + '?' + urlencode({**params, 'page': page})
        return {'previous': link(result['page'] - 1) if result['page'] > 1 else '',
                'following': link(result['page'] + 1) if result['page'] < result['pages'] else ''}

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc):
        if request.url.path.startswith(('/api/', '/health/')):
            return JSONResponse({'detail': exc.detail}, status_code=exc.status_code)
        mode = request.query_params.get('mode', settings.mode)
        mode = mode if mode in {'live', 'demo'} else settings.mode
        return HTMLResponse(render('error.html', **common(request, mode, '记录不可用', 'reports'),
            code=exc.status_code, message=exc.detail,
            description='记录可能不存在，或属于另一数据模式。可返回报告中心重新查找。'), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc):
        return await http_error(request, HTTPException(422, '筛选参数不正确，请调整后重试'))

    @app.get('/health/live')
    def live():
        return {'status': 'ok', 'mode': settings.mode}

    @app.get('/health/ready')
    def ready():
        h = app.state.harness
        credentials = settings.mode == 'demo' or bool(os.environ.get('DEEPSEEK_API_KEY') and os.environ.get('TAVILY_API_KEY'))
        with h.store.lock:
            integrity = h.store.db.execute('PRAGMA quick_check').fetchone()[0]
        return {'status': 'ready' if credentials and integrity == 'ok' else 'configuration_required', 'mode': settings.mode,
                'database': integrity, 'skills': len(h.skills.skills), 'credentials_configured': credentials,
                'schedule': settings.schedule.model_dump(), 'memory': h.memory.embedding_status}

    @app.get('/api/runs')
    def runs(mode: Mode | None = None, page: int = Query(1, ge=1), limit: int = Query(30, ge=1, le=100)):
        result = get_store(mode).report_catalog(page=page, page_size=limit)
        return [{k: v for k, v in row.items() if k != 'checkpoint'} for row in result['items']]

    @app.get('/api/report-catalog')
    def catalog(mode: Mode | None = None, page: int = Query(1, ge=1), q: str = Query('', max_length=200),
                status: str = '', kind: str = '', start: str = '', end: str = ''):
        store = get_store(mode)
        result = store.report_catalog(page=page, query=q, status=status, kind=kind, start=start, end=end)
        return {**result, 'items': [bundle_from_row(store, r) for r in result['items']]}

    @app.get('/api/runs/{run_id}')
    def run(run_id: str, mode: Mode | None = None):
        store, row = get_row(run_id, mode)
        row['checkpoint'] = json.loads(row['checkpoint'])
        row['usage'] = store.usage(run_id=run_id, mode=row['mode'])
        row['snapshot_at'] = row['updated_at']
        return row

    @app.get('/api/runs/{run_id}/report')
    def report_data(run_id: str, mode: Mode | None = None):
        store, row = get_row(run_id, mode)
        return bundle_from_row(store, row)

    @app.get('/api/runs/{run_id}/events')
    def events(run_id: str, mode: Mode | None = None, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)):
        store, _ = get_row(run_id, mode)
        return store.events(run_id, after=after, limit=limit)

    @app.get('/api/overview')
    def overview_api(mode: Mode | None = None):
        store = get_store(mode)
        usage = store.usage(day=store.billing_day(), mode=mode or settings.mode)
        return {**store.overview_counts(), 'today_cost': f"${usage['usd']:.4f}", 'today_search_credits': usage['search_credits']}

    @app.get('/api/skills')
    def skills_api():
        return app.state.harness.skills.catalog()

    @app.get('/api/memory')
    def memory_api(mode: Mode | None = None):
        store = get_store(mode)
        return {'latest_claims': store.latest_claims(20), 'open_tasks': [t.model_dump() for t in store.recall_tasks(100, include_not_due=True)]}

    @app.get('/', response_class=HTMLResponse)
    def dashboard(request: Request, mode: Mode | None = None):
        store = get_store(mode)
        selected = mode or settings.mode
        rows = store.report_catalog(kind='demo' if selected == 'demo' else 'research', page_size=5)['items']
        recent = [bundle_from_row(store, row) for row in rows]
        latest = recent[0] if recent else None
        today = store.billing_day()
        day_usage = store.usage(day=today, mode=selected)
        channels = latest['channels'] if latest else channel_summary({})
        broken = [c for c in channels if c['status'] in {'error', 'partial'}]
        alert = {'title': f'上次研究有 {len(broken)} 类信源需要关注', 'message': broken[0]['message'], 'day': latest['day']} if broken else None
        start = (date.fromisoformat(today) - timedelta(days=6)).isoformat()
        with store.lock:
            history = {r[0]: r[1] for r in store.db.execute("SELECT local_day,COUNT(*) FROM runs WHERE local_day>=? "
                "AND local_day<=? AND id NOT GLOB 'eval_*' AND id NOT GLOB 'accept_*' AND id NOT GLOB 'source-health*' GROUP BY local_day", (start, today))}
        maximum = max(history.values(), default=1) or 1
        trend = [{'day': (date.fromisoformat(start) + timedelta(days=i)).isoformat()} for i in range(7)]
        for point in trend:
            point['count'] = history.get(point['day'], 0)
            point['height'] = round(65 * point['count'] / maximum) if point['count'] else 3
        return render('overview.html', **common(request, mode, '研究总览', 'overview'), latest=latest, recent=recent,
            today=today, budget=settings.budget, day_usage=day_usage,
            budget_percent=min(100, round(100 * day_usage['usd'] / settings.budget.per_day_usd)),
            open_questions=store.recall_tasks(100, include_not_due=True), channels=channels, alert=alert,
            trend=trend, trend_total=sum(history.values()), today_runs=history.get(today, 0), poll_url='/api/overview?mode=' + selected)

    @app.get('/reports', response_class=HTMLResponse)
    def reports(request: Request, mode: Mode | None = None, page: int = Query(1, ge=1), q: str = Query('', max_length=200),
                status: str = '', kind: str = '', start: str = '', end: str = ''):
        error = ''
        for value in (start, end):
            if value:
                try:
                    date.fromisoformat(value)
                except ValueError as exc:
                    raise HTTPException(422, '日期格式不正确') from exc
        if start and end and start > end:
            error = '开始日期晚于结束日期，请调整筛选范围。'
        store = get_store(mode)
        result = store.report_catalog(page=page, query=q, status=status, kind=kind, start=start, end=end)
        return render('reports.html', **common(request, mode, '报告中心', 'reports'), result=result,
            reports=[bundle_from_row(store, row) for row in result['items']],
            filters={'q': q, 'status': status, 'kind': kind, 'start': start, 'end': end, 'error': error}, **paginate_urls(request, result))

    @app.get('/runs/{run_id}', response_class=HTMLResponse)
    @app.get('/runs/{run_id}/report', response_class=HTMLResponse)
    def report_page(request: Request, run_id: str, mode: Mode | None = None,
                    tab: Literal['report', 'evidence', 'trace', 'tasks'] = 'report', after: int = Query(0, ge=0)):
        store, row = get_row(run_id, mode)
        report = bundle_from_row(store, row)
        records = store.events(run_id, after=after, limit=101) if tab == 'trace' else []
        next_cursor = records[99]['seq'] if len(records) > 100 else None
        records = records[:100]
        for event in records:
            payload = event['payload']
            event['label'] = EVENT_LABELS.get(event['kind'], event['kind'])
            event['heading'] = str(payload.get('action') or payload.get('stage') or payload.get('source') or event['label'])
            event['summary'] = str(payload.get('reason') or payload.get('summary') or payload.get('message') or '')
        selected = mode or settings.mode
        return render('report.html', **common(request, mode, '报告阅读', 'reports'), report=report, tab=tab,
            events=records, next_cursor=next_cursor, snapshot=report['snapshot_at'],
            poll_url=f'/api/runs/{run_id}?mode={selected}' if row['status'] == 'running' else '')

    @app.get('/runs/{run_id}/export.{format}')
    def export_report(run_id: str, format: Literal['md', 'json', 'html', 'state.json'], mode: Mode | None = None, download: bool = True):
        store, row = get_row(run_id, mode)
        state = RunState.model_validate_json(row['checkpoint'])
        bundle = bundle_from_row(store, row)
        if format == 'json':
            content, mime = json.dumps(bundle, ensure_ascii=False, indent=2), 'application/json'
        elif format == 'state.json':
            content, mime = state.model_dump_json(indent=2), 'application/json'
        elif format == 'html':
            content, mime = standalone_report(bundle, settings.schedule.timezone), 'text/html'
        else:
            with tempfile.TemporaryDirectory(prefix='research-report-') as temporary:
                export_settings = settings.model_copy(update={'reports_dir': temporary})
                path = write_report(state, export_settings, store, row['status'])
                content = path.read_text()
            mime = 'text/markdown'
        headers = {'Content-Disposition': f'attachment; filename="{state.local_day}-{run_id}.{format}"'} if download else {}
        return Response(content, media_type=mime, headers=headers)

    @app.get('/evidence', response_class=HTMLResponse)
    def evidence(request: Request, mode: Mode | None = None, q: str = Query('', max_length=200),
                 verdict: Literal['', 'support', 'contradict', 'unverified'] = '', page: int = Query(1, ge=1),
                 tab: Literal['claims', 'questions'] = 'claims'):
        store = get_store(mode)
        result = store.query_claims(query=q, verdict=verdict, page=page)
        return render('evidence.html', **common(request, mode, '证据与记忆', 'evidence'), result=result, tab=tab,
            questions=store.recall_tasks(100, include_not_due=True), query=q, verdict=verdict, **paginate_urls(request, result))

    @app.get('/sources/{document_id}', response_class=HTMLResponse)
    def source_page(request: Request, document_id: str, mode: Mode | None = None):
        store = get_store(mode)
        if not re.fullmatch(r'doc_[a-zA-Z0-9_-]{1,80}', document_id):
            raise HTTPException(404, '找不到这份来源')
        documents = store.documents([document_id])
        if not documents:
            raise HTTPException(404, '找不到这份来源')
        with store.lock:
            related = store.db.execute('SELECT run_id FROM run_documents WHERE document_id=? ORDER BY rowid DESC LIMIT 1', (document_id,)).fetchone()
        selected = mode or settings.mode
        back = f'/runs/{related[0]}?mode={selected}&tab=evidence' if related else f'/evidence?mode={selected}'
        return render('source.html', **common(request, mode, '原文证据版本', 'evidence'), document=documents[0], back_link=back)

    @app.get('/skills', response_class=HTMLResponse)
    def skills_page(request: Request, mode: Mode | None = None):
        rows = get_store(mode).report_catalog(page_size=1)['items']
        loaded = json.loads(rows[0]['checkpoint'])['loaded_skills'] if rows else []
        skills = []
        for metadata in app.state.harness.skills.catalog():
            skill = app.state.harness.skills.get(metadata['name'])
            label, description = SKILL_LABELS.get(skill.name, (skill.name, skill.description))
            skills.append({**metadata, 'body': skill.body, 'label': label, 'description_zh': description, 'loaded': skill.name in loaded})
        return render('skills.html', **common(request, mode, 'Skills 与工具', 'skills'), skills=skills, tools=tool_catalog())

    @app.get('/about', response_class=HTMLResponse)
    def about(request: Request, mode: Mode | None = None):
        return render('about.html', **common(request, mode, '项目架构', 'overview'))

    return app
