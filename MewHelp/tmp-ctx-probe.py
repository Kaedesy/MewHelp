"""上下文管理实测探针：跑一段多轮对话，记录每轮滑窗 token、消息长度、摘要触发时刻。

只读不改：不写业务表以外的东西，用独立 user_id，跑完的会话数据留在库里可复查。
"""
import asyncio
import json

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.messages.utils import count_tokens_approximately

from app.config import settings
from app.core import memory
from app.db import repository
from app.graph import runtime

USER = "u_ctx_probe"

# 接近真实客服的一段：政策问答（知识路）+ 订单物流（工具）+ 跨轮指代 + 话题切换
QUESTIONS = [
    "你好，七天无理由退货是怎么算的",
    "那双跑鞋能退吗",
    "拆封了还能退吗",
    "退货邮费谁出",
    "我的订单 1001 现在什么状态",
    "它发货了吗",
    "物流到哪了",
    "大概什么时候能到",
    "换货和退货的流程一样吗",
    "换货一般要多久",
    "2002 这笔订单呢",
    "这单能开发票吗",
    "发票什么时候能拿到",
    "我之前问的那个跑鞋，最后到底能不能退",   # 跨轮指代，第 14 轮
    "退款一般多久到账",
    "会退回原来的支付方式吗",
    "如果那张卡已经注销了怎么办",
    "好的谢谢",
]


async def main():
    await runtime.init_graph()
    cid = None
    rows = []
    for i, q in enumerate(QUESTIONS, 1):
        out = await runtime.run_turn(USER, q, cid)
        cid = out["conversation_id"]
        st = out["state"] or {}
        if out.get("interrupt"):
            print(f"[{i:>2}] 触发中断，跳过续跑：{out['interrupt'].get('type')}")

        await asyncio.sleep(1.2)          # 给后台摘要任务一点时间落库
        conv = await repository.get_conversation(cid)
        upto = conv.summary_upto_msg_id or 0
        summary = conv.summary or ""

        msgs = st.get("messages", []) or []
        win = memory.build_window(msgs, upto, settings.context_window_max_tokens)
        win_tok = count_tokens_approximately(win)
        full_tok = count_tokens_approximately(msgs)

        answer = ""
        for m in reversed(msgs):
            if isinstance(m, AIMessage) and isinstance(m.content, str) and m.content:
                answer = m.content
                break

        rows.append({
            "turn": i, "route": st.get("route", ""), "intent": st.get("intent", ""),
            "user_chars": len(q), "ai_chars": len(answer),
            "msgs_total": len(msgs), "win_msgs": len(win),
            "win_tok": win_tok, "full_tok": full_tok,
            "summary_upto": upto, "summary_len": len(summary),
        })
        print(f"[{i:>2}] {st.get('route',''):<10} 滑窗 {win_tok:>5}tok/{len(win):>2}条  "
              f"全量 {full_tok:>5}tok/{len(msgs):>2}条  摘要边界={upto}  答复{len(answer)}字")

    print("\n最终摘要：", (await repository.get_conversation(cid)).summary or "(无)")
    with open("tmp-ctx-probe.json", "w", encoding="utf-8") as f:
        json.dump({"conversation_id": cid, "rows": rows}, f, ensure_ascii=False, indent=2)
    print("conversation_id =", cid)
    await runtime.close_graph()


if __name__ == "__main__":
    asyncio.run(main())
