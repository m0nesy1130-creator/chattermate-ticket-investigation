"""为模型请求分配上下文，证据始终使用原文或原文片段。"""

import json
import re
from collections.abc import Callable
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.request import urlopen


INPUT_TOKEN_BUDGET = 12_000
_TOKENIZER = Path(__file__).resolve().parents[1] / "runtime/deepseek-tokenizer/tokenizer.json"
_REVISION = "60d8d70770c6776ff598c94bb586a859a38244f1"
_DIGEST = "8f9f37ca37fdc4f5fd36d5cf4d3b0e8392edb4e894fd10cc0d70b4957c8633cf"
_LIST_KEYS = ("evidence", "dependencies", "turns", "observations", "excluded")


def json_text(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


@lru_cache(maxsize=1)
def _v4_tokenizer():
    from tokenizers import Tokenizer

    try:
        raw = _TOKENIZER.read_bytes() if _TOKENIZER.is_file() else b""
        if sha256(raw).hexdigest() != _DIGEST:
            url = f"https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash/resolve/{_REVISION}/tokenizer.json"
            with urlopen(url, timeout=12) as response:
                raw = response.read(7_000_000)
            if sha256(raw).hexdigest() != _DIGEST:
                return None
            _TOKENIZER.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=_TOKENIZER.parent, delete=False) as temporary:
                temporary.write(raw)
                filename = Path(temporary.name)
            try:
                filename.replace(_TOKENIZER)
            finally:
                filename.unlink(missing_ok=True)
        return Tokenizer.from_str(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None


def token_counter(model: str) -> Callable[[str], int]:
    tokenizer = _v4_tokenizer() if model in {"deepseek-flash", "deepseek-v4-pro"} else None
    if tokenizer is None:
        return lambda text: len(text.encode("utf-8"))
    return lambda text: len(tokenizer.encode(text, add_special_tokens=False).ids)


def _clip(text: str, count: Callable[[str], int], limit: int) -> str:
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if count(text[:middle]) <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def _rank(question: str, rows: list[dict]) -> list[dict]:
    if not question or len(rows) < 2:
        return rows
    try:
        from .retrieval import prepare_embedder

        vectors = list(prepare_embedder().embed([question[:800], *(row["text"][:800] for row in rows)]))
        scores = [float(vectors[0] @ vector) for vector in vectors[1:]]
        return [row for _, row in sorted(zip(scores, rows), key=lambda pair: -pair[0])]
    except (RuntimeError, OSError, ImportError, ValueError):
        tokens = set(re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", question.casefold()))
        return sorted(rows, key=lambda row: -sum(token in row["text"].casefold() for token in tokens))


def fit_context(
    system: str, data: dict[str, object], count: Callable[[str], int], budget: int = INPUT_TOKEN_BUDGET
) -> tuple[dict[str, object], int]:
    system_cost = count(system) + 64

    def cost(candidate: dict) -> int:
        return system_cost + count(json_text(candidate))

    full = cost(data)
    if full <= budget:
        return data, full

    pools = {key: data[key] for key in _LIST_KEYS if isinstance(data.get(key), (tuple, list))}
    result = {key: ([] if key in pools else value) for key, value in data.items()}
    if cost(result) > budget:
        raise ValueError("任务的固定上下文超过 token 预算，请缩小分析范围。")

    evidence = pools.get("evidence", ())
    cited = set(data.get("cited_ids", ()))
    mandatory = [row for row in evidence if row.get("id") in cited]
    optional = [row for row in evidence if row.get("id") not in cited]
    if mandatory:
        allowance = max(1, (budget - cost(result)) // len(mandatory) - 48)
        result["evidence"] = [{**row, "text": _clip(row["text"], count, allowance)} for row in mandatory]
        if cost(result) > budget or any(not row["text"] for row in result["evidence"]):
            raise ValueError("必须复核的引用超过 token 预算，请缩小分析范围。")

    question = str(data.get("question", ""))
    ordered = {
        "dependencies": list(pools.get("dependencies", ())),
        "evidence": _rank(question, optional),
        "observations": list(reversed(pools.get("observations", ()))),
        "turns": list(reversed(pools.get("turns", ()))),
        "excluded": list(pools.get("excluded", ())),
    }
    for key, rows in ordered.items():
        if key not in pools:
            continue
        for row in rows:
            if key == "evidence" and count(row["text"]) > 2_000:
                row = {**row, "text": _clip(row["text"], count, 2_000)}
            result[key].append(row)
            if cost(result) > budget:
                result[key].pop()
        if key in {"turns", "observations"}:
            result[key].reverse()
    return result, cost(result)


def keep_visible_references(result: dict, data: dict) -> dict:
    if "evidence" not in data:
        return result
    visible = {row.get("id") for row in data["evidence"]}
    refs = result.get("evidence_ids")
    if isinstance(refs, list) and all(isinstance(ref, str) for ref in refs) and not set(refs) <= visible:
        # 任一引用不可见时，整项结论交回复核判为缺证。
        result["evidence_ids"] = []
    checks = result.get("checks")
    if isinstance(checks, list):
        for item in checks:
            if not isinstance(item, dict):
                continue
            refs = item.get("evidence_ids")
            if isinstance(refs, list) and not all(isinstance(ref, str) and ref in visible for ref in refs):
                item["evidence_ids"] = []
    if isinstance(result.get("rejected"), list):
        result["rejected"] = [row for row in result["rejected"] if isinstance(row, dict)
                              and isinstance(ref := row.get("id", row.get("evidence_id")), str) and ref in visible]
    return result
