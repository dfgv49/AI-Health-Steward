# 本地改造说明（相对上游）

> 本仓库基于 MIT 协议的开源项目 [wangzhengpengjay/AI-Health-Steward](https://github.com/wangzhengpengjay/AI-Health-Steward) 二次开发。
> 本文件记录**相对上游的全部改动**，便于与上游同步、回滚或在面试中说明责任边界。
>
> **当前改动聚焦一件事：把报告检索从「单路向量」升级为「混合检索」。**

---

## 一、改动了什么（总览）

| 文件 | 类型 | 说明 |
|---|---|---|
| `backend/app/services/bm25_index.py` | **新增** | 按成员隔离的 BM25 关键词索引（jieba 分词 + rank_bm25） |
| `backend/app/services/rag.py` | 修改 | 检索改为「双路召回 → RRF 融合 → 阈值过滤」；归档后失效索引 |
| `backend/app/services/tools/query_reports.py` | 修改 | 返回归一化相关度分数；无证据时返回 `insufficient_evidence` |
| `backend/app/core/config.py` | 修改 | 新增 9 个检索相关配置项 |
| `backend/requirements.txt` | 修改 | 新增 `rank-bm25`、`jieba` |
| `backend/tests/test_retrieval.py` | **新增** | 13 个单元测试（RRF 数学 / 退化输入 / 归一化） |
| `.env.example` | 修改 | 补充检索相关配置示例 |
| `README.md` / `README_EN.md` | 修改 | 特性与技术栈同步 |
| `DEPLOYMENT.md` | 修改 | 新增「检索配置」小节 |
| `DEVELOPMENT.md` | 修改 | 后端结构补充检索模块 |

**未改动的部分**：Agent 循环、6 个工具的业务逻辑、多模态抽取链路、记忆机制、前端、数据库表结构。

---

## 二、要解决的问题

上游的报告检索是**单路向量检索**：

```sql
SELECT ... FROM report_chunks
WHERE member_id = :member_id
ORDER BY embedding <=> :query_vec
LIMIT :limit
```

**向量检索擅长语义匹配，不擅长精确匹配。** 而健康报告里恰恰大量是缩写与精确数值：

| 查询 | 单路向量的表现 |
|---|---|
| 「我肝功能怎么样」 | ✅ 语义匹配正常 |
| 「ALT 45 那次」 | ⚠️ 缩写 + 数值，召回弱 |
| 「HGB 是多少」 | ⚠️ 缩写匹配不上 |
| 「2026-07 的报告」 | ⚠️ 日期精确匹配弱 |

同时上游**没有分数阈值**——检索永远返回 top-k，库里没有的内容也会返回「最相似的几条」，模型拿到后容易硬答。

---

## 三、改造后的检索链路

```
query
  ├─→ 向量召回（pgvector 余弦距离）      → top-N  [(chunk_id, distance)]
  └─→ 关键词召回（jieba 分词 + BM25）    → top-N  [(chunk_id, bm25_score)]
                    ↓
            RRF 融合（只看排名，不看分数）
                    ↓
            阈值过滤（MIN_RELEVANCE）
                    ↓
        有证据 → 返回 results
        无证据 → 返回 insufficient_evidence，提示模型如实说明未找到
```

### 关键设计决策

| 决策 | 理由 |
|---|---|
| **用 RRF 而非加权求和** | 余弦距离（`[0,2]`）与 BM25 分数（无界）量纲不可比，加权前需脆弱归一化；RRF 只用排名 |
| **BM25 走进程内而非 PG 全文检索** | PG 中文 FTS 需 `pg_jieba`/`zhparser` 重建镜像；家庭场景仅数百 chunk，内存索引足够 |
| **索引按 `member_id` 构建** | 权限边界放在建索引阶段，避免「全局检索后过滤」把他人数据读进内存 |
| **归档后显式失效索引** | 仅靠 TTL 会让新归档报告在 5 分钟内检索不到 |
| **依赖缺失自动降级** | `rank-bm25`/`jieba` 安装失败时退回纯向量，不阻断主链路 |

### 相关度分数的变化

| | 上游 | 现在 |
|---|---|---|
| 返回字段 | `distance` | `score` |
| 范围 | `[0, 2]`（越小越相似） | **`[0, 1]`（越大越相关）** |
| 工具暴露 | `relevance = 1 - distance`（范围 `[-1, 1]`） | `relevance = score`（范围 `[0, 1]`） |

> ⚠️ 这是**破坏性契约变更**。`query_reports.py` 与 `rag.py` 的返回字段是一对，只改一边会 `KeyError`。

---

## 四、配置项

全部有默认值，**不配置也能启动**。

| 配置项 | 默认 | 说明 |
|---|---|---|
| `HYBRID_ENABLED` | `true` | 是否启用 BM25 关键词路 |
| `VECTOR_RECALL_N` | `20` | 向量路召回数 |
| `BM25_RECALL_N` | `20` | 关键词路召回数 |
| `RRF_K` | `60` | RRF 阻尼常数（越小越突出头部） |
| `MIN_RELEVANCE` | `0.0` | 证据阈值；`0` = 不过滤（保持上游行为） |
| `RERANK_ENABLED` | `false` | 交叉编码器重排（**预留，尚未接入**） |
| `RERANK_BACKEND` / `RERANK_API_BASE` / `RERANK_API_KEY` / `RERANK_MODEL` | 空 | 重排服务配置（预留） |

### 默认行为是安全的

```
HYBRID_ENABLED=true    → 召回变宽
RERANK_ENABLED=false   → 不做精排
MIN_RELEVANCE=0.0      → 不做过滤
```

**升级后答案不会突变，只是能召回到原来漏掉的内容。**

推荐部署顺序：**先开混合检索观察，再评测，最后才设阈值。**

---

## 五、验证

```bash
# 单元测试（不需要数据库）
docker compose exec backend python -m pytest tests/test_retrieval.py -v

# 依赖是否装上（返回 False 表示已降级为纯向量）
docker compose exec backend python -c "from app.services.bm25_index import deps_available; print(deps_available())"

# 索引构建日志
docker compose logs backend | grep 'BM25 index built'
```

> **部署注意**：`requirements.txt` 变更，**必须重建镜像**（`docker compose build backend`）；仅重启容器不会安装新依赖。

---

## 六、本次未做（后续方向）

| 事项 | 说明 |
|---|---|
| **交叉编码器重排** | 配置项已预留，实现代码未接入 |
| **阈值实际启用** | `MIN_RELEVANCE` 需先用标注集测定，否则会拒掉正常问题 |
| **报告切分** | 目前仍是「一份报告一个 chunk」，长报告语义被平均化 |
| **向量索引** | 未建 HNSW/IVFFlat，数据量大时 `<=>` 走全表扫描 |
| **效果评测** | 尚无标注查询集，recall@5 / NDCG@5 未测 |

---

## 七、补充改动：移除飞书渠道

**第二项改动**：删除上游的飞书（Lark）多渠道接入，改为 **WebUI 单入口**。

### 为什么删

- 飞书渠道需要企业自建应用与 WebSocket 长连接，个人自部署用不上；
- 它引入了 `lark-oapi` 依赖（约 3 个传递依赖），是本地开发环境里唯一装不上的包；
- 删除后代码量减少约 600 行，项目边界更清晰。

### 删了什么

| 对象 | 处理 |
|---|---|
| `backend/app/services/feishu.py`（406 行） | 整文件删除 |
| `backend/app/api/routes/feishu.py`（165 行） | 整文件删除 |
| `backend/app/models/feishu.py`（31 行） | 整文件删除 |
| `backend/app/main.py` | 去掉 lifespan 中启动/停止 Bot 的调用 |
| `backend/app/api/v1/router.py` | 去掉路由挂载 |
| `backend/app/models/__init__.py` | 去掉 `FeishuChannel` 导出 |
| `backend/app/core/config.py` | 删 4 个 `FEISHU_*` 配置项 |
| `backend/requirements.txt` | 删 `lark-oapi` |
| `frontend/src/pages/Settings.tsx` | 删飞书渠道管理 UI 与 ChannelModal |
| `frontend/src/lib/api.ts` | 删 `feishuApi` 与相关类型 |
| `.env.example` | 删 `FEISHU_*` 示例 |
| 文档 | README / README_EN / OVERVIEW / DEPLOYMENT / DEVELOPMENT / PRIVACY 同步 |

### ⚠️ 有意保留的部分

| 保留项 | 原因 |
|---|---|
| `backend/alembic/versions/e5f6a7b8c9d0_feishu_channels.py` | **已执行的迁移不能删**，否则迁移链断裂（后续 6 个迁移依赖它） |
| 数据库表 `feishu_channels` | 空表，删除需新增迁移；无模型映射后不影响运行 |
| `settings.py` 的 `_CONFIG_ONLY_TABLES` 含 `feishu_channels` | 保证数据导出/清空时不误删该表 |
| `tests/test_settings_data.py` 的相关断言 | 同上，验证该表不参与数据清除 |

> **如需彻底删表**，新增一个迁移执行 `op.drop_table("feishu_channels")` 即可，代码层已无依赖。

---

## 八、与上游同步

上游为独立仓库。同步方式二选一：

```bash
# 方式一：加 remote 后 merge（保 history）
git remote add upstream https://github.com/wangzhengpengjay/AI-Health-Steward.git
git fetch upstream
git merge upstream/main

# 方式二：手动对比（改动集中在 6 个文件，见第一节表格）
```

**合并时最易冲突的文件**：`backend/app/services/rag.py`（检索主逻辑）、`backend/app/core/config.py`（配置追加）。
**合并时最易误恢复的文件**：被删除的三个 `feishu.py`、`main.py` 的 lifespan、`api/v1/router.py` 的路由挂载。

---

## 九、许可与署名

上游项目采用 **MIT License**（Copyright (c) 2026 AI Health Steward Contributors）。
本仓库保留原始 `LICENSE` 与版权声明，改动部分同样以 MIT 协议提供。
