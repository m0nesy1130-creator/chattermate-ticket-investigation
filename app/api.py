"""本地控制台使用的任务 HTTP 接口。"""

import re
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from langgraph.types import StateSnapshot
from langsmith import tracing_context
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .adaptive import describe_source
from .checkpoints import TaskBusyError, mysql_checkpointer
from .clarification import Clarification, ClarificationTurn
from .contracts import Evidence, ReviewStatus
from .database_query import query_sources
from .directories import DirectoryInputError, DirectorySource, load_directory
from .documents import DOCUMENT_SUFFIXES, MAX_DOCUMENT_BYTES, evidence_from_bytes
from .payments import payment_scope, payment_sources
from .planning import latest_results
from .tasks import (
    Mode,
    TaskConflictError,
    TaskInputError,
    TaskNotFoundError,
    finish_with_current_evidence,
    initial_state,
    input_receipt,
    load_snapshot,
    normalize_project,
    pending_kind,
    report_from_state,
    resume_with_clarification,
    resume_with_document,
    task_graph,
)


UPLOAD_DIR = Path(__file__).resolve().parents[1] / "runtime" / "uploads"
CONSOLE_DIR = Path(__file__).with_name("web")

app = FastAPI(title="analysis-agent", description="创建和恢复本地文档分析任务。")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
app.mount("/assets", StaticFiles(directory=CONSOLE_DIR), name="console-assets")


@app.get("/", include_in_schema=False)
def console() -> FileResponse:
    return FileResponse(CONSOLE_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.middleware("http")
async def require_same_origin(request: Request, call_next):
    origin = request.headers.get("origin")
    if request.method == "POST" and origin and origin != str(request.base_url).rstrip("/"):
        return JSONResponse({"detail": "请从本地服务的同源页面提交任务。"}, status_code=403)
    return await call_next(request)


class StepView(BaseModel):
    id: str
    question: str
    depends_on: tuple[str, ...]
    status: ReviewStatus | Literal["pending", "blocked", "partial"]
    claim: str | None = None
    references: tuple[str, ...] = ()


class PaymentSourceView(BaseModel):
    id: str
    label: str
    database: str
    table: str


class PaymentView(BaseModel):
    source: PaymentSourceView
    contract_id: str
    billing_period: str


class QuerySourceView(PaymentSourceView):
    key: str
    columns: list[str]


class TaskView(BaseModel):
    task_id: str
    status: Literal["clarifying", "awaiting_input", "incomplete", "completed"]
    question: str
    mode: Mode
    hybrid: bool
    project: str | None = None
    review_status: ReviewStatus | None = None
    prompt: str | None = None
    receipt: str | None = None
    report_url: str | None = None
    clarification: Clarification | None = None
    clarification_turns: list[ClarificationTurn] = Field(default_factory=list)
    confirmed_scope: str | None = None
    steps: list[StepView] = Field(default_factory=list)
    can_finish: bool = False
    directory: DirectorySource | None = None
    payment: PaymentView | None = None
    adaptive: bool = False
    web_enabled: bool = False
    query_source: QuerySourceView | None = None
    observations: list[dict] = Field(default_factory=list)


class ClarificationReply(BaseModel):
    receipt: str = Field(min_length=1, max_length=160)
    action: Literal["answer", "confirm"]
    answer: str | None = Field(default=None, max_length=4000)
    direction: int | None = Field(default=None, ge=0)


def _step_views(state, completed: bool) -> list[StepView]:
    plan = state.get("plan_steps", ())
    if not plan:
        return []
    results = latest_results(state.get("step_results", []))
    verified = {key for key, result in results.items() if result.review.status == "supported"}
    views = []
    for step in plan:
        result = results.get(step.id)
        if result is not None:
            status = result.review.status
        else:
            status = "pending" if verified.issuperset(step.depends_on) else "blocked"
        claim = result.claim if result is not None and status in {"supported", "inference"} else None
        views.append(StepView(
            id=step.id, question=step.question, depends_on=step.depends_on, status=status,
            claim=claim.text if claim else None, references=claim.evidence_ids if claim else (),
        ))
    summary_status = "pending"
    if completed:
        summary_status = "supported" if len(verified) == len(plan) else "partial"
    views.append(StepView(
        id="summary", question="汇总各步骤结果", depends_on=tuple(step.id for step in plan), status=summary_status,
    ))
    return views


def _task_view(thread_id: str, snapshot: StateSnapshot) -> TaskView:
    state = snapshot.values
    review = state.get("review")
    waiting = pending_kind(snapshot)
    if waiting == "clarification":
        status = "clarifying"
    elif waiting == "supplement":
        status = "awaiting_input"
    elif snapshot.next:
        status = "incomplete"
    else:
        status = "completed"
    source = state.get("query_source")
    return TaskView(
        task_id=thread_id,
        status=status,
        question=state.get("original_question", state["request"].question),
        mode=state["mode"],
        hybrid=state.get("hybrid", False),
        project=state.get("project"),
        review_status=review.status if review is not None else None,
        prompt=snapshot.interrupts[0].value if waiting == "supplement" else None,
        receipt=input_receipt(snapshot) if waiting else None,
        report_url=f"/tasks/{thread_id}/report" if status == "completed" else None,
        clarification=state["clarification"] if waiting == "clarification" else None,
        clarification_turns=state.get("clarification_turns", []),
        confirmed_scope=state["request"].question if "original_question" in state else None,
        steps=_step_views(state, status == "completed"),
        can_finish=waiting == "supplement" and bool(state.get("plan_steps")),
        directory=state.get("directory"),
        payment=state.get("payment"),
        adaptive=state.get("adaptive", False),
        web_enabled=state.get("web_enabled", False),
        query_source=QuerySourceView(id=source["id"], **describe_source(source)) if source else None,
        observations=state.get("tool_observations", []),
    )


@contextmanager
def _task_session(thread_id: str):
    config = {"configurable": {"thread_id": thread_id}}
    try:
        with tracing_context(enabled=False), mysql_checkpointer(thread_id) as checkpointer:
            yield checkpointer, config
    except TaskNotFoundError as error:
        raise HTTPException(404, str(error)) from error
    except TaskInputError as error:
        raise HTTPException(422, str(error)) from error
    except (TaskConflictError, TaskBusyError) as error:
        raise HTTPException(409, str(error)) from error
    except (ValueError, RuntimeError) as error:
        raise HTTPException(503, {"message": str(error), "task_id": thread_id}) from error
    except OSError as error:
        raise HTTPException(500, {"message": "无法读取或保存本地文件。", "task_id": thread_id}) from error


def _console_snapshot(graph, config) -> StateSnapshot:
    snapshot = load_snapshot(graph, config)
    if snapshot.values.get("bridge_fingerprint"):
        raise TaskNotFoundError("找不到该控制台任务。")
    return snapshot


def _save_upload(thread_id: str, upload: UploadFile) -> tuple[Evidence, ...]:
    filename = (upload.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    suffix = Path(filename).suffix.lower()
    if suffix not in DOCUMENT_SUFFIXES:
        raise HTTPException(422, "请上传 UTF-8 编码的 .md 或 .txt 文件。")
    raw = upload.file.read(MAX_DOCUMENT_BYTES + 1)
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise HTTPException(413, "文件大小超过 2 MiB。")

    stem = re.sub(r"[^\w.-]+", "-", Path(filename).stem)[:60].strip(".-") or "document"
    target = UPLOAD_DIR / thread_id / sha256(raw).hexdigest() / f"upload-{stem}{suffix}"
    try:
        evidence = evidence_from_bytes(raw, str(target.resolve()))
    except UnicodeDecodeError as error:
        raise HTTPException(422, "文件无法按 UTF-8 读取。") from error
    if not evidence:
        raise HTTPException(422, "上传文档没有可读内容。")

    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with NamedTemporaryFile(dir=target.parent, delete=False) as temp:
            temp_path = Path(temp.name)
            temp.write(raw)
        temp_path.replace(target)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return evidence


@app.get("/payment-sources", summary="列出已配置的付款台账")
def list_payment_sources() -> list[PaymentSourceView]:
    try:
        return [PaymentSourceView(**source) for source in payment_sources().values()]
    except RuntimeError as error:
        raise HTTPException(503, str(error)) from error


@app.get("/query-sources", summary="列出可查询的 MySQL 表")
def list_query_sources() -> list[QuerySourceView]:
    try:
        return [QuerySourceView(id=source["id"], **describe_source(source)) for source in query_sources().values()]
    except RuntimeError as error:
        raise HTTPException(503, str(error)) from error


@app.post("/tasks", status_code=201, summary="创建分析任务")
def create_task(
    question: Annotated[str, Form(min_length=1, max_length=4000)],
    file: Annotated[UploadFile | None, File()] = None,
    live: Annotated[bool, Form()] = False,
    hybrid: Annotated[bool, Form()] = False,
    project: Annotated[str | None, Form()] = None,
    clarify: Annotated[bool, Form()] = False,
    multistep: Annotated[bool, Form()] = False,
    directory: Annotated[str | None, Form(max_length=4096)] = None,
    include_subdirectories: Annotated[bool, Form()] = False,
    payment_source: Annotated[str | None, Form(max_length=32)] = None,
    contract_id: Annotated[str | None, Form(max_length=40)] = None,
    billing_period: Annotated[str | None, Form(max_length=40)] = None,
    adaptive: Annotated[bool, Form()] = False,
    web_enabled: Annotated[bool, Form()] = False,
    query_source: Annotated[str | None, Form(max_length=32)] = None,
) -> TaskView:
    question = question.strip()
    if not question:
        raise HTTPException(422, "问题不能为空。")
    try:
        project = normalize_project(project)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    if not live and (hybrid or project is not None or multistep or payment_source or adaptive or web_enabled or query_source):
        raise HTTPException(422, "所选检索和分析选项需要启用 DeepSeek。")
    if payment_source and (adaptive or web_enabled or query_source):
        raise HTTPException(422, "固定付款核对与开放检索需要分别创建任务。")
    selected_query = None
    if query_source is not None:
        try:
            selected_query = query_sources().get(query_source)
        except RuntimeError as error:
            raise HTTPException(503, str(error)) from error
        if selected_query is None:
            raise HTTPException(422, "请选择已配置的 MySQL 查询表。")
    if payment_source is None and (contract_id or billing_period):
        raise HTTPException(422, "请先选择付款台账。")
    payment = None
    if payment_source is not None:
        try:
            payment = payment_scope(payment_source, contract_id, billing_period)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except RuntimeError as error:
            raise HTTPException(503, str(error)) from error
    if include_subdirectories and directory is None:
        raise HTTPException(422, "请选择资料目录后再启用包含子目录。")

    directory_source = None
    directory_evidence = ()
    if directory is not None:
        try:
            directory_source, directory_evidence = load_directory(directory, recursive=include_subdirectories)
        except DirectoryInputError as error:
            raise HTTPException(422, str(error)) from error

    thread_id = uuid4().hex
    mode = "live" if live else "offline"
    with _task_session(thread_id) as (checkpointer, config):
        graph = task_graph(mode, checkpointer)
        document = _save_upload(thread_id, file) if file is not None else ()
        state = initial_state(
            question, (*document, *directory_evidence), mode=mode, hybrid=hybrid,
            project=project, clarify=clarify, multistep=multistep or payment is not None,
            directory=directory_source, payment=payment,
            adaptive=adaptive, web_enabled=web_enabled, query_source=selected_query,
        )
        graph.invoke(state, config, durability="sync")
        return _task_view(thread_id, load_snapshot(graph, config))


@app.get("/tasks/{task_id}", summary="查询任务状态")
def get_task(task_id: UUID) -> TaskView:
    with _task_session(task_id.hex) as (checkpointer, config):
        snapshot = _console_snapshot(task_graph("offline", checkpointer), config)
        return _task_view(task_id.hex, snapshot)


@app.post("/tasks/{task_id}/clarification", summary="回答需求追问或确认分析范围")
def clarify_task(task_id: UUID, reply: ClarificationReply) -> TaskView:
    with _task_session(task_id.hex) as (checkpointer, config):
        snapshot = _console_snapshot(task_graph("offline", checkpointer), config)
        graph = task_graph(snapshot.values["mode"], checkpointer)
        resume_with_clarification(
            graph, config, snapshot, reply.receipt,
            action=reply.action, answer=reply.answer, direction=reply.direction,
        )
        return _task_view(task_id.hex, load_snapshot(graph, config))


@app.post("/tasks/{task_id}/supplement", summary="提交补充文档并续跑")
def supplement_task(
    task_id: UUID,
    receipt: Annotated[str, Form(min_length=1, max_length=160)],
    file: Annotated[UploadFile | None, File()] = None,
    finish: Annotated[bool, Form()] = False,
) -> TaskView:
    if finish == (file is not None):
        raise HTTPException(422, "请上传一份补充文档，或选择用现有证据结束任务。")
    with _task_session(task_id.hex) as (checkpointer, config):
        snapshot = _console_snapshot(task_graph("offline", checkpointer), config)
        graph = task_graph(snapshot.values["mode"], checkpointer)
        if file is not None:
            resume_with_document(graph, config, snapshot, receipt, lambda: _save_upload(task_id.hex, file))
        else:
            finish_with_current_evidence(graph, config, snapshot, receipt)
        return _task_view(task_id.hex, load_snapshot(graph, config))


@app.post("/tasks/{task_id}/retry", summary="继续因执行故障而中断的任务")
def retry_task(task_id: UUID) -> TaskView:
    with _task_session(task_id.hex) as (checkpointer, config):
        snapshot = _console_snapshot(task_graph("offline", checkpointer), config)
        if snapshot.interrupts:
            raise TaskConflictError("当前任务正在等待输入。请先处理待确认内容或补充材料。")
        if snapshot.next:
            graph = task_graph(snapshot.values["mode"], checkpointer)
            graph.invoke(None, config, durability="sync")
            snapshot = load_snapshot(graph, config)
        return _task_view(task_id.hex, snapshot)


@app.get("/tasks/{task_id}/report", summary="下载 Markdown 报告", response_class=Response)
def download_report(task_id: UUID) -> Response:
    with _task_session(task_id.hex) as (checkpointer, config):
        snapshot = _console_snapshot(task_graph("offline", checkpointer), config)
        if snapshot.interrupts or snapshot.next:
            raise TaskConflictError("任务完成后才能下载报告。")
        return Response(
            report_from_state(snapshot.values),
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="analysis-{task_id.hex}.md"'},
        )
