# 存储架构与演进

## 设计结论

AGI Yukino 的数据层不是“把所有东西都塞进 SQLite”，也不是把同一份事实同时写进三种数据库。它按职责分成三层：

1. **事实层**：本机使用 SQLite + WAL，服务化使用 PostgreSQL。会话、记忆正文、任务状态、权限、幂等键、审计、向量原文和图谱实体/关系只以这一层为准。
2. **检索层**：小规模时由 SQLite/Python 或 PostgreSQL + pgvector 完成；需要独立扩缩容时启用 Milvus。Milvus 只保存可从事实层重建的向量投影。
3. **关系层**：关系表始终保存图谱事实；需要多跳遍历、图算法或可视化时启用 Neo4j。Neo4j 同样只是可重建投影。

Redis 只适合跨实例通知、短期限流和缓存，不保存任务、权限或记忆事实。当前也不引入 Elasticsearch/Kafka：现有关系过滤与本地词法通道足够，等压测证明词法吞吐或事件吞吐成为瓶颈后再增加组件。

```mermaid
flowchart LR
    Agent["Agent / Web / Worker"] --> Contract["领域 Repository"]
    Contract --> Facts["事实层\nSQLite WAL / PostgreSQL"]
    Facts --> LocalVector["小规模向量\nJSON cosine / pgvector"]
    Facts --> GraphFacts["会话隔离图谱关系表"]
    Facts -. "幂等重建" .-> Milvus["Milvus\n语义索引投影"]
    GraphFacts -. "幂等重建" .-> Neo4j["Neo4j\n属性图投影"]
    Agent -. "可丢失协调" .-> Redis["Redis（可选）"]
```

## 数据归属

| 领域 | 事实来源 | 可选投影 | 一致性规则 |
|---|---|---|---|
| 会话、画像、关系、长期记忆 | SQLite / PostgreSQL | 无 | 会话隔离；清除必须事务完成 |
| Agent 任务、步骤、事件、检查点 | SQLite / PostgreSQL | 无 | 状态迁移、预算、租约与取消原子化 |
| 权限、确认单、外发回执 | SQLite / PostgreSQL | 无 | 幂等、扣次、确认和对账事务化 |
| 记忆向量 | `memory_embeddings` | pgvector 或 Milvus | 关系候选 ID + session_id 双重过滤；删除正文同时删向量 |
| 知识图谱 | `knowledge_entities`、`knowledge_relations` | Neo4j | 稳定 ID、会话隔离、证据哈希、幂等 MERGE |
| 附件正文 | 受控本地目录 | S3 兼容对象存储 | 数据库只存元数据和访问边界 |
| 在线状态、通知、短缓存 | 进程内 | Redis | 可过期、可重建、不得影响事实正确性 |

Milvus 检索不会直接信任索引中的全部结果。运行时先从关系事实层取出当前会话仍然存在的候选 ID，再以 `session_id + candidate_ids` 过滤 Milvus 搜索，因此旧索引、误同步或其他会话的数据不会进入回答。Milvus 故障时自动回退 pgvector 或本地余弦检索。

Neo4j 中的节点和边使用稳定 UID 与参数化 `MERGE` 写入，不依赖 APOC，也不把实体名称设为全局唯一。投影失败不会阻止关系事实落库；关系检索仍可从事实层工作，之后可显式重建 Neo4j。

## 本机与服务化形态

本机默认无需外部数据库：

```bash
agi-yukino storage status --json
agi-yukino storage vector-status --json
agi-yukino storage graph-status --json
```

服务化事实库通过单一进程级开关启用，不能给单个模块各配一套事实库：

```bash
export YUKINO_DATABASE_URL='postgresql://user:password@host:5432/agi_yukino'
agi-yukino serve
```

地址无效或 PostgreSQL 不可用时启动直接失败，不会偷偷退回 SQLite。迁移前可预检，备份后再显式执行：

```bash
pip install -e '.[storage]'
export YUKINO_POSTGRES_DSN='postgresql://user:password@host:5432/agi_yukino'
agi-yukino storage migrate-control-plane --dry-run --json
agi-yukino storage migrate-control-plane --confirmed --json
```

迁移器覆盖并校验 **38 张**关系型事实表，保留主键、状态、租约、幂等键和版本字段；重跑不会覆盖目标端更新版本。导入 PostgreSQL 后，JSON 向量会回填 pgvector 列。

## Milvus 与 Neo4j

项目提供独立的存储服务编排，不把数据库进程塞进应用容器：

```bash
export YUKINO_MINIO_PASSWORD='请使用强随机密码'
export YUKINO_NEO4J_BOOTSTRAP_PASSWORD='请使用强随机密码'
docker compose -f docker-compose.storage.yml up -d
```

随后在页面“设置 → 记忆数据库”配置连接，也可使用环境变量：

```bash
export OPENAI_EMBEDDING_MODEL='text-embedding-3-small'
# 聊天供应商没有 Embedding 接口时，使用独立连接：
export OPENAI_EMBEDDING_API_KEY='...'
export OPENAI_EMBEDDING_BASE_URL='https://api.openai.com/v1'
export YUKINO_VECTOR_BACKEND='milvus'
export YUKINO_MILVUS_URI='http://127.0.0.1:19531'

export YUKINO_GRAPH_BACKEND='neo4j'
export YUKINO_NEO4J_URI='neo4j://127.0.0.1:7688'
export YUKINO_NEO4J_USERNAME='neo4j'
export YUKINO_NEO4J_PASSWORD='...'
```

配置切换后先做连接测试，再从事实层重建投影：

```bash
agi-yukino storage rebuild-vectors --drop-existing --confirmed --json
agi-yukino storage rebuild-graph --clear-existing --confirmed --json
```

清空外部投影不会删除事实层。反方向则不同：用户忘记一条记忆或清除会话时，关系事实、向量事实、Milvus 索引、图谱事实和 Neo4j 投影会按同一会话/证据范围删除。

## 与参考项目的边界

这里只借鉴了“PostgreSQL 保存事实、向量库做召回、图数据库做扩展、外部组件故障可降级”的思想。实现没有复制 agi-saber 的类、配置或代码，并进一步增加了：

- 所有图实体和边的会话隔离，而不是全局名称唯一；
- 关系事实优先落库，Milvus/Neo4j 可随时删除并幂等重建；
- Milvus 搜索受事实层候选 ID 约束，避免陈旧索引越权召回；
- 忘记/清除链路同步删除向量与图谱证据；
- 密钥只从设置或环境读取，状态输出脱敏；
- 无 APOC 依赖、无源码内明文凭据、无隐式 Lite 降级；
- Doctor、页面与 CLI 都暴露未探测、已连接和故障状态。

生产切换仍应在独立环境完成并发、断网恢复、备份恢复和索引重建演练。Milvus/Neo4j 的正确角色是加速和扩展查询，而不是让系统多出两个无法仲裁的事实源。
