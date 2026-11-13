"""命令行入口：批量运行 RoBERTa 主题分桶和证据约束的故障初诊。"""
import argparse
import asyncio
import json

from app.core.diagnosis_analysis import process_pending_diagnoses


async def main(limit: int, batch_size: int) -> None:
    # 具体分类、模型调用、校验和持久化由 core 模块负责。
    stats = await process_pending_diagnoses(limit=limit, batch_size=batch_size)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--batch-size", type=int, default=10)
    args = parser.parse_args()
    asyncio.run(main(args.limit, args.batch_size))
