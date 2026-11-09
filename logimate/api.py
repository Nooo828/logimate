import hmac
import os
import re
import threading
import secrets
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.exceptions import HTTPException as StarletteHTTPException

from .models import DiagnoseRequest, Diagnosis
from .repository import Repository
from .rules import diagnose
from pydantic import BaseModel, ConfigDict, Field
from .resilient_worker import SearchArguments
from .cases import Actor, CaseError, CaseStore, CaseService, CreateCase, ReviewCase
from uuid import UUID


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=1000)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=500)
    mode: Literal['keyword','dense','hybrid'] = 'hybrid'
    top_k: int = Field(default=5, ge=1, le=10)
    domain: Literal['order','logistics','warehouse','aftersales'] | None = None


class PolicyGraphRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=500)


class StartReviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    case_id: UUID


def create_app(repository: Repository | None = None, token: str | None = None, *, resilient_service=None, shared_url=None,
               case_store=None, review_token=None, native_review_service=None,
               analysis_runner_factory=None, analysis_planner_mode='live'):
    repo = repository or Repository()
    expected = token or os.environ.get("LOGIMATE_DEMO_TOKEN", "local-demo-token")
    # One ephemeral capability for this demo-a API process. Never exposed in responses/model prompts.
    internal_token = secrets.token_urlsafe(32)
    shared_connection = {'url': shared_url or 'http://127.0.0.1:8000/internal/p6/search', 'token': internal_token}
    reviewer_token = review_token or os.environ.get('LOGIMATE_REVIEW_TOKEN', '')
    case_service = None
    try:
        if case_store is not None or os.environ.get('LOGIMATE_CASE_DB'):
            if reviewer_token and hmac.compare_digest(reviewer_token, expected): raise ValueError('review token must be separate')
            case_service = CaseService(case_store or CaseStore(os.environ['LOGIMATE_CASE_DB']), repo)
    except (CaseError, OSError): pass  # Disabled/unavailable is reported by case routes, never an in-memory fallback.
    native_service = native_review_service
    if native_service is None and case_service is not None and os.environ.get('LOGIMATE_NATIVE_REVIEW')=='1':
        try:
            from .native_review import NativeReview
            native_service = NativeReview(case_service.store)
        except (ImportError, CaseError, OSError): pass
    resilient_state = {'service': None, 'enabled': resilient_service is not None or os.environ.get('LOGIMATE_RESILIENT_RETRIEVAL') == '1',
                       'initialization_error': None}
    analysis_service = None
    if case_service is not None and native_service is not None and (analysis_runner_factory is not None or os.environ.get('LOGIMATE_ANALYSIS_REVIEW')=='1'):
        from .analysis_review import AnalysisReview
        def prepare_analysis():
            from .deepseek import DeepSeek, ModelUnavailable
            from .five_agent import run_tool_graph
            if resilient_state['service'] is None:raise ModelUnavailable('Shared retrieval is not enabled',category='configuration')
            provider=DeepSeek()
            def execute(query,tenant):
                result=run_tool_graph(provider,query,tenant,repo,with_knowledge=True,shared_retrieval=shared_connection)
                import json
                secret=getattr(provider,'_key','')
                if secret and secret in json.dumps(result,ensure_ascii=False):raise CaseError(503,'ANALYSIS_SENSITIVE_OUTPUT_WITHHELD')
                return result
            return execute
        analysis_service=AnalysisReview(case_service.store,native_service,repo,analysis_runner_factory or prepare_analysis,
            planner_mode=analysis_planner_mode)

    @asynccontextmanager
    async def lifespan(app):
        if resilient_state['enabled']:
            try:
                if resilient_service is not None: service = resilient_service
                else:
                    from .resilient_retrieval import ResilientRetrieval
                    service = ResilientRetrieval()
                resilient_state['service'] = service
                await service.start()
            except (ValueError, RuntimeError, OSError, ImportError):
                resilient_state['initialization_error'] = 'initialization_unavailable'
        try: yield
        finally:
            service = resilient_state['service']
            if service is not None: await service.close()
            resilient_state['service'] = None

    app = FastAPI(title="LogiMate 独立复现（模拟数据）", version="0.1.0", lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)
    search_state = {'service': None}
    search_lock = threading.Lock()

    def identity(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if not credentials or not hmac.compare_digest(credentials.credentials, expected):
            raise HTTPException(401, "需要有效演示凭证")
        return "demo-a"  # Server-controlled identity; never accept a tenant from request body.

    def reviewer(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if not reviewer_token or not credentials or not hmac.compare_digest(credentials.credentials, reviewer_token):
            raise HTTPException(401, '需要独立的人工审核演示凭证')
        return Actor('demo-a', 'demo-reviewer', True)

    def cases():
        if case_service is None: raise HTTPException(503, '持久工单未启用或存储不可用')
        return case_service

    @app.exception_handler(CaseError)
    async def case_error(request, exc):
        return JSONResponse(status_code=exc.status, content={'simulated':True,'error':{'code':exc.code}})

    @app.post('/v1/cases')
    def create_case(body: CreateCase, tenant=Depends(identity), key: str | None = Header(default=None, alias='Idempotency-Key')):
        result = cases().create(Actor(tenant, 'demo-operator'), body.model_dump(), key)
        return JSONResponse(status_code=200 if result['replayed'] else 201, content=result)

    @app.get('/v1/cases/{case_id}')
    def get_case(case_id: UUID, tenant=Depends(identity)):
        return cases().store.get(str(case_id), tenant)

    @app.post('/v1/cases/{case_id}/decisions')
    def review_case(case_id: UUID, body: ReviewCase, actor=Depends(reviewer),
                    key: str | None = Header(default=None, alias='Idempotency-Key')):
        return cases().store.decide(actor, str(case_id), body.model_dump(), key)

    def native_call(method, *args):
        if native_service is None: raise HTTPException(503,'原生审核持久服务未启用或依赖不可用')
        try: return getattr(native_service,method)(*args)
        except CaseError: raise
        except Exception: raise HTTPException(503,'原生审核暂不可用，已持久化的请求保持待恢复') from None

    @app.post('/v1/review-threads')
    def start_review_thread(body: StartReviewRequest, tenant=Depends(identity),
                            key: str | None = Header(default=None,alias='Idempotency-Key')):
        return native_call('start',Actor(tenant,'demo-operator'),str(body.case_id),key)

    @app.get('/v1/review-threads/{thread_id}')
    def get_review_thread(thread_id: UUID, tenant=Depends(identity)):
        return native_call('get',str(thread_id),tenant)

    @app.post('/v1/review-threads/{thread_id}/resume')
    def resume_review_thread(thread_id: UUID, body: ReviewCase, actor=Depends(reviewer),
                             key: str | None = Header(default=None,alias='Idempotency-Key')):
        return native_call('resume',actor,str(thread_id),body.model_dump(),key)

    def analysis_call(method,*args):
        if analysis_service is None:raise HTTPException(503,'分析与审核绑定服务未启用')
        from .deepseek import ModelUnavailable
        try:return getattr(analysis_service,method)(*args)
        except CaseError:raise
        except ModelUnavailable:raise HTTPException(503,'模型或共享检索尚未配置，未发起新的分析') from None
        except Exception:raise HTTPException(503,'分析绑定未完成；请按幂等键查询记录，未记录调用不会自动重试') from None

    from .analysis_review import AnalysisRequest
    @app.post('/v1/graph/mcp/review/queries')
    def analysis_review_query(body:AnalysisRequest,tenant=Depends(identity),
                              key:str|None=Header(default=None,alias='Idempotency-Key')):
        return analysis_call('analyze',Actor(tenant,'demo-operator'),body.model_dump(),key)

    @app.get('/v1/analysis-reviews/by-key/{key}')
    def analysis_review_by_key(key:str,tenant=Depends(identity)):
        return analysis_call('get_by_key',Actor(tenant,'demo-operator'),key)

    @app.get('/v1/analysis-reviews/{analysis_id}')
    def analysis_review_result(analysis_id:UUID,tenant=Depends(identity)):
        return analysis_call('get',str(analysis_id),tenant)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        return JSONResponse(status_code=exc.status_code,
                            content={"simulated": True, "error": {"code": str(exc.status_code), "message": exc.detail}})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(status_code=422, content={"simulated": True, "error": {
            "code": "VALIDATION_ERROR", "message": "输入不符合接口契约",
            "fields": [{"location": list(e["loc"]), "type": e["type"]} for e in exc.errors()]}})

    @app.get("/health")
    def health():
        return {"status": "ok", "simulated": True, "implementation": "rules-v1",
                "dataset_id": repo.dataset.dataset_id, "dataset_sha256": repo.sha256}

    def lookup(order_id, tenant):
        if not re.fullmatch(r"SIM-[0-9]{3}", order_id):
            raise HTTPException(422, "单号格式应为SIM-三位数字")
        snapshot = repo.get(order_id, tenant)
        if snapshot is None:
            raise HTTPException(404, "未找到可访问的模拟订单")
        return snapshot

    @app.get("/v1/orders/{order_id}")
    def get_order(order_id: str, tenant=Depends(identity)):
        return {"simulated": True, "dataset_id": repo.dataset.dataset_id,
                "as_of": repo.dataset.as_of, "records": lookup(order_id, tenant)}

    @app.post("/v1/diagnoses", response_model=Diagnosis)
    def diagnosis(body: DiagnoseRequest, tenant=Depends(identity)):
        return diagnose(lookup(body.order_id, tenant), as_of=repo.dataset.as_of,
                        dataset_id=repo.dataset.dataset_id, intent=body.intent)

    @app.post("/v1/agent/queries")
    def agent_query(body: AgentRequest, tenant=Depends(identity)):
        from .deepseek import DeepSeek, ModelUnavailable
        from .single_agent import run_agent
        try:
            provider = DeepSeek()
        except ModelUnavailable:
            raise HTTPException(503, "模型尚未配置，不能执行单Agent请求")
        result = run_agent(provider, body.query, tenant, repo)
        if result["status"] == "unavailable":
            return JSONResponse(status_code=503, content=result)
        return result

    @app.post("/v1/graph/queries")
    def graph_query(body: AgentRequest, tenant=Depends(identity)):
        from .deepseek import DeepSeek, ModelUnavailable
        from .five_agent import run_five_agent
        try:
            provider = DeepSeek()
        except ModelUnavailable:
            raise HTTPException(503, "模型尚未配置，不能执行五Agent请求")
        result = run_five_agent(provider, body.query, tenant, repo)
        if result["status"] == "unavailable":
            return JSONResponse(status_code=503, content=result)
        return result

    @app.post("/v1/graph/mcp/queries")
    def mcp_graph_query(body: AgentRequest, tenant=Depends(identity)):
        from .deepseek import DeepSeek, ModelUnavailable
        from .five_agent import run_tool_graph
        try:
            provider = DeepSeek()
        except ModelUnavailable:
            raise HTTPException(503, "模型尚未配置，不能执行MCP五Agent请求")
        result = run_tool_graph(provider, body.query, tenant, repo)
        if result["status"] == "unavailable":
            return JSONResponse(status_code=503, content=result)
        return result

    @app.post('/v1/graph/mcp/knowledge/queries')
    def policy_graph_query(body: PolicyGraphRequest, tenant=Depends(identity)):
        from .deepseek import DeepSeek, ModelUnavailable
        from .five_agent import run_tool_graph
        try: provider = DeepSeek()
        except ModelUnavailable:
            raise HTTPException(503, '模型尚未配置，不能执行知识增强五Agent请求')
        result = run_tool_graph(provider, body.query, tenant, repo, with_knowledge=True)
        return JSONResponse(status_code=503 if result['status'] == 'unavailable' else 200, content=result)

    @app.get('/v1/knowledge/resilient/health')
    async def resilient_health(tenant=Depends(identity)):
        service = resilient_state['service']
        return {'simulated': True, **(service.snapshot() if service is not None else {
            'enabled': resilient_state['enabled'], 'ready': False,
            'initialization_error': resilient_state['initialization_error']})}

    @app.post('/internal/p6/search', include_in_schema=False)
    async def internal_shared_search(body: SearchArguments, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if not credentials or not hmac.compare_digest(credentials.credentials, internal_token):
            raise HTTPException(401, '内部检索凭证无效')
        service = resilient_state['service']
        if service is None: raise HTTPException(503, '共享检索尚不可用')
        result = await service.search(body, 'demo-a')
        return JSONResponse(status_code=result.status_code, content=result.body)

    @app.post('/v1/graph/mcp/resilient/queries')
    def shared_policy_graph_query(body: PolicyGraphRequest, tenant=Depends(identity)):
        if resilient_state['service'] is None: raise HTTPException(503, '共享检索未启用')
        from .deepseek import DeepSeek, ModelUnavailable
        from .five_agent import run_tool_graph
        try: provider = DeepSeek()
        except ModelUnavailable: raise HTTPException(503, '模型尚未配置，不能执行共享检索五Agent请求')
        result = run_tool_graph(provider, body.query, tenant, repo, with_knowledge=True, shared_retrieval=shared_connection)
        return JSONResponse(status_code=503 if result['status'] == 'unavailable' else 200, content=result)

    @app.post('/v1/knowledge/resilient/search')
    async def resilient_search(body: SearchArguments, tenant=Depends(identity)):
        service = resilient_state['service']
        if service is None: raise HTTPException(503, '检索可靠性服务未启用或尚不可用')
        result = await service.search(body, tenant)
        return JSONResponse(status_code=result.status_code, content=result.body)

    @app.post('/v1/knowledge/search')
    def knowledge_search(body: SearchRequest, tenant=Depends(identity)):
        from .retrieval import KnowledgeSearch
        try:
            # Serialize model initialization/CPU access in this minimal single-worker implementation.
            with search_lock:
                if search_state['service'] is None: search_state['service'] = KnowledgeSearch()
                return search_state['service'].search(body.query, tenant, mode=body.mode, top_k=body.top_k, domain=body.domain)
        except (ImportError, OSError, ValueError, RuntimeError):
            raise HTTPException(503, '模拟知识检索暂不可用，请检查服务配置或稍后重试')

    return app


app = create_app()
