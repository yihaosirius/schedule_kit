-- 记录一次抽取走的是哪条通道：tool_call 还是 json_fallback。
--
-- 降级必须是**响亮**的。不记下来的话，"模型偶尔没按工具调用返回、于是悄悄
-- 走了 JSON"会一直藏在日志里，直到某天变成常态才被发现——而那时已经不知道
-- 是从什么时候开始退化的了。确认页也会把它标出来。

ALTER TABLE ingest_drafts ADD COLUMN llm_path TEXT;
