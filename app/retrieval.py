"""在当前任务的证据内做关键词与本地向量检索。"""

import json
from functools import lru_cache
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from tempfile import NamedTemporaryFile

try:
    import numpy as np
    from fastembed import TextEmbedding
    from huggingface_hub import snapshot_download
except ImportError:
    raise RuntimeError("混合检索需要安装 requirements.txt 中的 FastEmbed。") from None

from .contracts import Evidence
from .documents import find_evidence

MODEL_NAME = "BAAI/bge-small-zh-v1.5"
MODEL_REPOSITORY = "Qdrant/bge-small-zh-v1.5"
MODEL_REVISION = "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59"
MODEL_FILES = (
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "model_optimized.onnx",
)
MODEL_DIMENSION = 512
# 用内置逐行资料和补证样例测得的初始候选门槛。
MIN_SIMILARITY = 0.65
MAX_NEIGHBOR_LINES = 2
INDEX_DIR = Path(__file__).resolve().parents[1] / "runtime" / "rag"


@lru_cache(maxsize=1)
def prepare_embedder() -> TextEmbedding:
    model_dir = INDEX_DIR / "models" / MODEL_REVISION
    try:
        if not all((model_dir / filename).is_file() for filename in MODEL_FILES):
            snapshot_download(
                MODEL_REPOSITORY,
                revision=MODEL_REVISION,
                allow_patterns=list(MODEL_FILES),
                local_dir=model_dir,
                token=False,
            )
        return TextEmbedding(
            model_name=MODEL_NAME,
            cache_dir=str(INDEX_DIR / "models"),
            specific_model_path=str(model_dir),
            providers=["CPUExecutionProvider"],
            threads=2,
        )
    except Exception as error:
        raise RuntimeError("本地 BGE 模型加载失败，请检查模型缓存和下载连接。") from error


def _document_vectors(evidence: tuple[Evidence, ...]) -> np.ndarray:
    digest = sha256(f"{MODEL_REVISION}:{version('fastembed')}:1".encode("utf-8"))
    for item in evidence:
        digest.update(json.dumps((item.version, item.line, item.text), ensure_ascii=False).encode("utf-8"))
    cache = INDEX_DIR / "vectors" / f"{digest.hexdigest()}.npy"
    expected_shape = (len(evidence), MODEL_DIMENSION)

    try:
        with cache.open("rb") as saved:
            vectors = np.load(saved, allow_pickle=False)
        if (
            isinstance(vectors, np.ndarray)
            and vectors.shape == expected_shape
            and vectors.dtype == np.float32
            and np.isfinite(vectors).all()
        ):
            return vectors
    except (OSError, ValueError, EOFError):
        pass

    vectors = np.asarray(
        list(prepare_embedder().embed((item.text for item in evidence), batch_size=32)),
        dtype=np.float32,
    )
    if vectors.shape != expected_shape or not np.isfinite(vectors).all():
        raise RuntimeError("本地模型未为所有证据返回有效向量。")

    cache.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with NamedTemporaryFile(dir=cache.parent, suffix=".npy", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            np.save(temporary, vectors, allow_pickle=False)
        temporary_path.replace(cache)
    except PermissionError:
        # Windows 下另一个进程可能正在读取已写好的同版本索引。
        if not cache.exists():
            raise
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return vectors


def find_hybrid_evidence(
    keyword: str, question: str, evidence: tuple[Evidence, ...], limit: int = 5
) -> tuple[Evidence, ...]:
    if not evidence or limit < 1:
        return ()
    exact_matches = find_evidence(keyword, evidence, limit)
    query_vector = next(prepare_embedder().embed(question))
    similarities = _document_vectors(evidence) @ query_vector
    semantic_matches = (
        evidence[index] for index in np.argsort(-similarities, kind="stable")[:limit]
        if similarities[index] >= MIN_SIMILARITY
    )
    candidates = {item.evidence_id: item for item in (*exact_matches, *semantic_matches)}
    hits = tuple(candidates.values())[:limit]
    return (*hits, *_neighbor_lines(hits, evidence))


def _neighbor_lines(hits: tuple[Evidence, ...], evidence: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
    if not hits:
        return ()
    by_location = {(item.source, item.version, item.line): item for item in evidence if item.line is not None}
    known = {item.evidence_id for item in hits}
    neighbors = []
    for hit in hits:
        if hit.line is None:
            continue
        for offset in (-1, 1):
            neighbor = by_location.get((hit.source, hit.version, hit.line + offset))
            if neighbor is not None and neighbor.evidence_id not in known:
                neighbors.append(neighbor)
                known.add(neighbor.evidence_id)
                if len(neighbors) == MAX_NEIGHBOR_LINES:
                    return tuple(neighbors)
    return tuple(neighbors)
