"""构建不可变知识快照，并管理经过评估和审批的发布与回滚。"""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.core.persisted_tasks import canonical_hash
from app.core import embeddings
from app.db import repository
from app.kb import milvus_client

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_DIR = ROOT / "data/ch11/snapshots"
RUNTIME_CONFIG_FIELDS = (
    "chat_model", "chat_base_url", "intent_model", "intent_base_url",
    "summary_model", "summary_base_url", "embed_model", "embed_base_url",
    "rerank_model", "rerank_base_url", "recall_top_k", "rerank_top_k",
    "subquery_split", "rerank_min_score", "evidence_confidence_threshold",
)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str).encode("utf-8")


def _write_immutable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"immutable snapshot artifact differs: {path}")
        return
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(payload)
    temp.replace(path)


def _kb_sources() -> tuple[dict[str, str], str]:
    source_root = ROOT / "data/kb"
    file_hashes: dict[str, str] = {}
    digest = hashlib.sha256()
    for path in sorted(source_root.glob("*.md")):
        relative = path.relative_to(ROOT).as_posix()
        content = path.read_bytes()
        file_hashes[relative] = hashlib.sha256(content).hexdigest()
        digest.update(path.name.encode("utf-8"))
        digest.update(content)
    return file_hashes, digest.hexdigest()


def runtime_config(overrides: dict | None = None) -> dict:
    result = {name: getattr(settings, name) for name in RUNTIME_CONFIG_FIELDS}
    if overrides:
        allowed = {"recall_top_k", "rerank_top_k", "subquery_split"}
        for name, value in overrides.items():
            if name == "split":
                result["subquery_split"] = value
                continue
            if name in allowed:
                result[name] = value
    return result


async def create_snapshot(created_by: str, batch_size: int = 64) -> dict:
    """保存知识源、分块和索引配置，并重建独立的 Milvus 集合。"""
    if not created_by.strip() or batch_size < 1:
        raise ValueError("created_by is required and batch_size must be positive")
    chunks = await repository.list_vectorized_chunks()
    if not chunks:
        raise ValueError("cannot snapshot an empty vectorized knowledge base")
    file_hashes, source_sha = _kb_sources()
    chunks_sha = hashlib.sha256(_json_bytes(chunks)).hexdigest()
    index_config = {
        "schema_version": "hybrid-v1", "embedding_model": settings.embed_model,
        "embedding_endpoint": settings.embed_base_url, "vector_dimension": milvus_client.DIM,
        "text_fields": ["category", "questions", "answer"],
        "chunk_source_sha256": chunks_sha, "source_markdown_sha256": source_sha,
    }
    snapshot_id = "ks_" + canonical_hash({"files": file_hashes, "index": index_config})[:24]
    collection = f"knowledge_{snapshot_id}"
    manifest = {
        "schema_version": 1, "snapshot_id": snapshot_id,
        "collection_name": collection, "source_files": file_hashes,
        "source_markdown_sha256": source_sha, "chunks_sha256": chunks_sha,
        "chunk_count": len(chunks), "index_config": index_config,
    }
    manifest_bytes = _json_bytes(manifest)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    artifact_dir = SNAPSHOT_DIR / snapshot_id
    await repository.save_knowledge_snapshot(
        snapshot_id=snapshot_id, collection_name=collection,
        manifest_sha256=manifest_sha, manifest=manifest,
        created_by=created_by, status="building")

    _write_immutable(artifact_dir / "manifest.json", manifest_bytes)
    _write_immutable(artifact_dir / "chunks.json", _json_bytes(chunks))
    for relative in file_hashes:
        source_path = ROOT / Path(relative)
        _write_immutable(artifact_dir / "sources" / Path(relative).name,
                         source_path.read_bytes())

    client = milvus_client.get_client()
    await milvus_client.acall(milvus_client.ensure_collection, client, collection)
    try:
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start:start + batch_size]
            texts = [f"{row['category']}\n{row['questions']}\n{row['answer']}"
                     for row in batch]
            vectors = await embeddings.embed_texts(texts)
            rows = [{
                "id": row["id"], "dense": vector, "text": text,
                "question": row["questions"], "answer": row["answer"],
                "section_path": row["section_path"] or "",
                "content_type": row["content_type"] or "",
                "category": row["category"] or "",
            } for row, vector, text in zip(batch, vectors, texts, strict=True)]
            await milvus_client.acall(milvus_client.upsert_vectors, client, rows, collection)
        await milvus_client.acall(milvus_client.flush, client, collection)
        await milvus_client.acall(client.load_collection, collection)
        actual_count = await milvus_client.acall(milvus_client.count, client, collection)
        if actual_count != len(chunks):
            raise RuntimeError(f"snapshot row count mismatch: expected {len(chunks)}, got {actual_count}")
    except Exception:
        await repository.save_knowledge_snapshot(
            snapshot_id=snapshot_id, collection_name=collection,
            manifest_sha256=manifest_sha, manifest=manifest,
            created_by=created_by, status="failed")
        raise
    await repository.save_knowledge_snapshot(
        snapshot_id=snapshot_id, collection_name=collection,
        manifest_sha256=manifest_sha, manifest=manifest,
        created_by=created_by, status="ready")
    return {**manifest, "manifest_sha256": manifest_sha, "status": "ready"}


def _assert_compatible(manifest: dict, config: dict) -> None:
    index = manifest.get("index_config") or {}
    if index.get("embedding_model") != config.get("embed_model"):
        raise ValueError("snapshot index embedding model is incompatible with query embedding model")
    if index.get("embedding_endpoint") != config.get("embed_base_url"):
        raise ValueError("snapshot embedding endpoint differs from runtime query endpoint")
    if index.get("vector_dimension") != milvus_client.DIM:
        raise ValueError("snapshot vector dimension does not match the runtime index schema")
    for name in ("chat_base_url", "intent_base_url", "summary_base_url", "rerank_base_url"):
        saved, environment = config.get(name), getattr(settings, name)
        if saved and environment and saved != environment:
            raise ValueError(
                f"release requires {name}={saved}; set matching credentials/endpoint before activation"
            )


async def activate_snapshot(*, snapshot_id: str, candidate_report_path: Path,
                            gate_report_path: Path, candidate_name: str,
                            approved_by: str, actor: str | None = None) -> dict:
    """检查评估报告、发布门禁和人工审批后，创建并激活一个发布组合。"""
    if not approved_by.strip():
        raise ValueError("human approval identity is required")
    snapshot = await repository.get_knowledge_snapshot(snapshot_id)
    if not snapshot or snapshot["status"] != "ready":
        raise ValueError("knowledge snapshot is missing or not ready")
    report_bytes = candidate_report_path.read_bytes()
    gate = json.loads(gate_report_path.read_text(encoding="utf-8"))
    report = json.loads(report_bytes.decode("utf-8"))
    expected_report_sha = hashlib.sha256(report_bytes).hexdigest()
    if gate.get("status") != "PASS":
        raise ValueError("release gate report must have status PASS")
    if (gate.get("candidate_report_sha256") != expected_report_sha
            or gate.get("candidate_name") != candidate_name):
        raise ValueError("release gate report does not match this candidate report/name")
    meta = report.get("meta") or {}
    if meta.get("dataset_role") != "test" or meta.get("knowledge_version") != snapshot_id:
        raise ValueError("candidate report must use independent test data and this snapshot ID")
    if meta.get("collection_name") != snapshot["collection_name"]:
        raise ValueError("candidate report was not evaluated against this snapshot collection")
    if meta.get("knowledge_source_sha256") != snapshot["manifest"].get("source_markdown_sha256"):
        raise ValueError("candidate report knowledge source hash does not match the snapshot")
    candidate = (report.get("candidates") or {}).get(candidate_name)
    if not candidate or candidate.get("status") != "complete":
        raise ValueError("candidate evaluation is missing or incomplete")
    current_models = {"embed": settings.embed_model, "rerank": settings.rerank_model,
                      "chat": settings.chat_model}
    if meta.get("models") != current_models:
        raise ValueError("candidate report model versions do not match the current runtime")
    config = runtime_config(candidate.get("config"))
    _assert_compatible(snapshot["manifest"], config)
    config_sha = hashlib.sha256(_json_bytes(config)).hexdigest()
    config_version = "sc_" + config_sha[:24]
    await repository.save_system_config_version(
        config_version=config_version, config_sha256=config_sha,
        config=config, created_by=approved_by)
    release_id = str(uuid.uuid4())
    evaluation = {
        "candidate_name": candidate_name,
        "candidate_report_sha256": expected_report_sha,
        "gate_report_sha256": hashlib.sha256(gate_report_path.read_bytes()).hexdigest(),
        "dataset_sha256": meta.get("dataset_sha256"),
        "dataset_role": meta.get("dataset_role"),
    }
    await repository.create_evolution_release(
        release_id=release_id, snapshot_id=snapshot_id,
        config_version=config_version, evaluation=evaluation,
        approved_by=approved_by)
    client = milvus_client.get_client()
    await milvus_client.acall(milvus_client.activate_collection, client,
                              snapshot["collection_name"])
    previous = await repository.set_active_release(
        release_id=release_id, actor=actor or approved_by,
        reason=f"approved candidate {candidate_name} on snapshot {snapshot_id}")
    return {"release_id": release_id, "previous_release_id": previous,
            "snapshot_id": snapshot_id, "collection_name": snapshot["collection_name"],
            "config_version": config_version, "restart_required": True}


async def rollback_release(*, release_id: str, actor: str, reason: str) -> dict:
    """恢复指定发布；先验证模型兼容性和目标索引完整性。"""
    if not actor.strip() or not reason.strip():
        raise ValueError("rollback actor and reason are required")
    target = await repository.get_evolution_release(release_id)
    if not target:
        raise ValueError("release does not exist")
    if (target["snapshot_manifest"].get("snapshot_id") != target["snapshot_id"]):
        raise ValueError("release snapshot manifest identity mismatch")
    _assert_compatible(target["snapshot_manifest"], target["config"])
    client = milvus_client.get_client()
    actual_count = await milvus_client.acall(milvus_client.count, client,
                                             target["collection_name"])
    expected_count = target["snapshot_manifest"].get("chunk_count")
    if actual_count != expected_count:
        raise ValueError("target snapshot index is incomplete; rollback refused")
    await milvus_client.acall(milvus_client.activate_collection, client,
                              target["collection_name"])
    previous = await repository.set_active_release(
        release_id=release_id, actor=actor, reason=reason, event_type="rollback")
    return {"active_release_id": release_id, "previous_release_id": previous,
            "snapshot_id": target["snapshot_id"],
            "collection_name": target["collection_name"], "restart_required": True}


async def apply_active_release_config() -> str | None:
    """Load a published config at process startup; API keys remain environment-only."""
    active_id = await repository.get_active_release_id()
    if not active_id:
        return None
    release = await repository.get_evolution_release(active_id)
    if not release:
        raise RuntimeError(f"active release {active_id} has missing catalog records")
    _assert_compatible(release["snapshot_manifest"], release["config"])
    client = milvus_client.get_client()
    if not await milvus_client.acall(client.has_collection, release["collection_name"]):
        raise RuntimeError("active release points to a missing Milvus snapshot collection")
    await milvus_client.acall(milvus_client.activate_collection, client,
                              release["collection_name"])
    for name, value in release["config"].items():
        if name in RUNTIME_CONFIG_FIELDS:
            setattr(settings, name, value)
    return active_id
