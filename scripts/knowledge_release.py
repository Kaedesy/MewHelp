"""命令行管理知识快照、经过审批的发布激活及版本回滚。"""
import argparse
import asyncio
import json
from pathlib import Path

from app.core import knowledge_snapshots


async def run(args) -> None:
    if args.command == "snapshot":
        result = await knowledge_snapshots.create_snapshot(args.created_by)
    elif args.command == "activate":
        result = await knowledge_snapshots.activate_snapshot(
            snapshot_id=args.snapshot_id,
            candidate_report_path=args.candidate_report,
            gate_report_path=args.gate_report,
            candidate_name=args.candidate,
            approved_by=args.approved_by,
        )
    else:
        result = await knowledge_snapshots.rollback_release(
            release_id=args.release_id, actor=args.actor, reason=args.reason)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else Path(__file__).resolve().parents[1] / path


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--created-by", required=True)
    activate = commands.add_parser("activate")
    activate.add_argument("--snapshot-id", required=True)
    activate.add_argument("--candidate-report", required=True, type=_path)
    activate.add_argument("--gate-report", required=True, type=_path)
    activate.add_argument("--candidate", required=True)
    activate.add_argument("--approved-by", required=True)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--release-id", required=True)
    rollback.add_argument("--actor", required=True)
    rollback.add_argument("--reason", required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
