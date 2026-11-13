"""为结构化模型任务提供版本标识、持久化调用记录和失败重试。"""
import hashlib
import json
import uuid
from typing import Awaitable, Callable, TypeVar

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel

from app.config import settings
from app.db import repository

_T = TypeVar("_T", bound=BaseModel)
TASK_SCHEMA_VERSION = "v1"
MAX_TASK_ATTEMPTS = 3


def canonical_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def analysis_config_version(prompt: ChatPromptTemplate, schema: type[BaseModel]) -> str:
    messages = []
    for message in prompt.messages:
        template = getattr(getattr(message, "prompt", None), "template", None)
        messages.append({"role": getattr(message, "role", type(message).__name__),
                         "template": template or str(message)})
    return canonical_hash({
        "model": settings.chat_model,
        "base_url": settings.chat_base_url,
        "temperature": 0.3,
        "max_output_tokens": settings.max_output_tokens,
        "prompt": messages,
        "schema": schema.model_json_schema(),
    })


def persisted_task_id(*, task_type: str, input_payload: object,
                      prompt: ChatPromptTemplate, schema: type[BaseModel]) -> str:
    """将任务类型、输入、提示词/模型配置和输出结构合成稳定任务 ID。"""
    return canonical_hash({
        "task_type": task_type,
        "input_fingerprint": canonical_hash(input_payload),
        "analysis_config_version": analysis_config_version(prompt, schema),
        "output_schema_version": f"{schema.__name__}:{TASK_SCHEMA_VERSION}",
    })


async def run_persisted_task(
    *, task_type: str,
    input_payload: object,
    prompt: ChatPromptTemplate,
    schema: type[_T],
    call_type: str,
    invoke: Callable[[], Awaitable[_T]],
) -> tuple[_T, bool]:
    """优先复用已完成结果，否则调用模型并保存结果；返回本次是否实际调用。"""
    input_fingerprint = canonical_hash(input_payload)
    config_version = analysis_config_version(prompt, schema)
    schema_version = f"{schema.__name__}:{TASK_SCHEMA_VERSION}"
    task_id = persisted_task_id(task_type=task_type, input_payload=input_payload,
                                prompt=prompt, schema=schema)

    saved = await repository.get_flywheel_task(task_id)
    if saved and saved["execution_status"] == "completed" and saved["result_json"] is not None:
        return schema.model_validate(saved["result_json"]), False
    if saved and saved["attempts"] >= MAX_TASK_ATTEMPTS:
        await repository.finish_flywheel_task(
            task_id, status="failed", error_type="retry_limit_exceeded")
        raise RuntimeError(f"persisted task {task_id} exhausted retries")

    await repository.start_flywheel_task(
        task_id=task_id, task_type=task_type,
        input_fingerprint=input_fingerprint,
        analysis_config_version=config_version,
        output_schema_version=schema_version,
    )
    invocation_id = str(uuid.uuid4())
    await repository.start_flywheel_invocation(
        invocation_id=invocation_id, task_id=task_id, call_type=call_type)
    try:
        result = await invoke()
        result_json = result.model_dump(mode="json")
        response_hash = canonical_hash(result_json)
        await repository.finish_flywheel_execution(
            invocation_id, task_id, task_status="completed",
            result_json=result_json, response_hash=response_hash)
        return result, True
    except Exception as exc:
        status = ("failed" if saved and saved["attempts"] + 1 >= MAX_TASK_ATTEMPTS
                  else "retryable")
        await repository.finish_flywheel_execution(
            invocation_id, task_id, task_status=status,
            error_type=type(exc).__name__)
        raise
