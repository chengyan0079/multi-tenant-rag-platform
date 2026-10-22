"""
qa_router - 问答路由层（M9）：FAQ 优先 + RAG 兜底

流程（"缓存 → 命中? → 阈值路由 → 兜底"骨架，升级为 bge-m3）：
① 答案缓存命中 → 直接返回
② FAQ 检索(scope=1) top-1 相似度 ≥ 阈值 → 取 faq_qa 标准答案
③ 未命中 → RAG 检索(scope=0) → Rerank → M8 生成
"""
