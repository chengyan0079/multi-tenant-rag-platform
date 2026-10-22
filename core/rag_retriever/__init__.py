"""
rag_retriever - 混合检索模块（M4）

两路召回（bge-m3 dense cosine + sparse IP）→ RRF 融合 → Rerank 精排 → 父块扩展/来源标注。
检索只产 RetrievedChunk；生成交给 M8（LangChain）。
"""
