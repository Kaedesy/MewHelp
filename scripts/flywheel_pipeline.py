# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
"""数据飞轮命令行入口：扫描未归并问题，标准化并送入人工审核队列。
运行:make flywheel(需 mysql + 聊天上游在线)。定时跑给 cron 示例:
  */30 * * * * cd /path/to/mewhelp && make flywheel >> log/flywheel.log 2>&1
"""
import asyncio
import sys

from app.core.flywheel import process_pending


async def main():
    # 该流程只生成/更新待审候选，不会绕过人工审核直接写入知识库。
    stats = await process_pending(limit=200)
    print(f"Recovered persisted tasks: {stats['recovered_tasks']}")
    print(f"Ignored low-quality reports: {stats['ignored']}")
    print(f"本轮处理 {stats['processed']} 条:新建缺口 {stats['created']},"
          f"归并 {stats['merged']},跳过待重试 {stats['skipped']};"
          f"归并调用 {stats['model_calls']},复核调用 {stats['audit_calls']}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
