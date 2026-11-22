"""在独立项目名下验证记忆在原文更新后失效。"""

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from app.checkpoints import mysql_connection
from app.contracts import Claim
from app.documents import find_evidence, load_evidence
from app.memory import recall_evidence, remember_supported


def memory_case():
    project = f"eval-holdout-{uuid4().hex}"

    def search(rows):
        return find_evidence("防尘网", rows)

    with TemporaryDirectory(prefix="analysis-agent-holdout-") as folder:
        file = Path(folder) / "synthetic-service.txt"
        file.write_text("虚构仓库保养计划\n北岸库防尘网每 28 日更换一次。\n", encoding="utf-8")
        document = load_evidence(file)
        original = document[1]
        claim = Claim("防尘网每 28 日更换一次。", (original.evidence_id,))
        remember_supported(project, "防尘网多久更换？", claim, (original,), document,
                           datetime.now(timezone.utc).isoformat())
        try:
            before = recall_evidence(project, (), search)
            file.write_text("虚构仓库保养计划\n北岸库防尘网每 35 日更换一次。\n", encoding="utf-8")
            current = load_evidence(file)[1]
            after = recall_evidence(project, (), search)
        finally:
            with mysql_connection() as (connection, _), connection.cursor() as cursor:
                cursor.execute("DELETE FROM analysis_memories WHERE project = %s", (project,))

    original_recalled = len(before) == 1 and before[0].version == original.version and before[0].text == original.text
    changed = current.version != original.version
    omitted = not after
    return {"passed": original_recalled and changed and omitted, "original_recalled": original_recalled,
            "source_version_changed": changed, "stale_memory_omitted": omitted}
