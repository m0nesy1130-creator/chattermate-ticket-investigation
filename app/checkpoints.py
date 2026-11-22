"""为分析任务提供内存和 MySQL 检查点的序列化与连接配置。"""

import os
from contextlib import contextmanager
from hashlib import sha256

import pymysql
from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from .contracts import (
    Claim, Evidence, Money, Plan, PlanStep, RejectedDirection, Review, ReviewItem, StepResult, TaskRequest,
)
from .credentials import LOCAL_MYSQL, MYSQL_CREDENTIAL, read_credential


class TaskBusyError(RuntimeError):
    """另一个进程持有任务锁或数据库初始化锁。"""


def checkpoint_serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(
        allowed_msgpack_modules=[
            (cls.__module__, cls.__name__)
            for cls in (TaskRequest, Plan, Evidence, Claim, Review, ReviewItem, PlanStep, StepResult, Money, RejectedDirection)
        ]
    )


@contextmanager
def _mysql_lock(connection, name: str, timeout: int):
    with connection.cursor() as cursor:
        cursor.execute("SELECT GET_LOCK(%s, %s)", (name, timeout))
        if cursor.fetchone()[0] != 1:
            raise TaskBusyError("另一进程正在使用此任务或初始化检查点，请稍后再试。")
    try:
        yield
    finally:
        if connection.open:
            with connection.cursor() as cursor:
                cursor.execute("SELECT RELEASE_LOCK(%s)", (name,))


@contextmanager
def mysql_connection():
    default_host, default_port, default_user, default_database = LOCAL_MYSQL
    user = os.environ.get("ANALYSIS_AGENT_MYSQL_USER", default_user)
    password = os.environ.get("ANALYSIS_AGENT_MYSQL_PASSWORD")
    database = os.environ.get("ANALYSIS_AGENT_MYSQL_DATABASE", default_database)
    if not (user and database):
        raise ValueError("MySQL 账号名和库名不能为空。")

    host = os.environ.get("ANALYSIS_AGENT_MYSQL_HOST", default_host)
    try:
        port = int(os.environ.get("ANALYSIS_AGENT_MYSQL_PORT", str(default_port)))
    except ValueError:
        raise ValueError("ANALYSIS_AGENT_MYSQL_PORT 必须是整数。") from None
    if not 1 <= port <= 65535:
        raise ValueError("ANALYSIS_AGENT_MYSQL_PORT 必须是 1 到 65535 之间的端口。")
    if not password and (host, port, user, database) == LOCAL_MYSQL:
        password = read_credential(MYSQL_CREDENTIAL)
    if not password:
        raise ValueError("请设置 ANALYSIS_AGENT_MYSQL_PASSWORD，或运行 python -m app.credentials 配置本机凭据。")
    try:
        with pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password.encode("utf-8"),
            database=database,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=5,
            read_timeout=15,
            write_timeout=15,
        ) as connection:
            yield connection, database
    except pymysql.MySQLError as error:
        code = error.args[0] if error.args and isinstance(error.args[0], int) else "未知"
        raise RuntimeError(f"MySQL 操作失败（错误码 {code}）。") from None


@contextmanager
def mysql_checkpointer(thread_id: str):
    with mysql_connection() as (connection, database):
        schema_key = sha256(database.casefold().encode("utf-8")).hexdigest()[:12]
        saver = PyMySQLSaver(connection, serde=checkpoint_serializer())
        with _mysql_lock(connection, f"analysis-agent:{schema_key}:setup", 10):
            saver.setup()
        with _mysql_lock(connection, f"analysis-agent:{schema_key}:{thread_id}", 0):
            yield saver
