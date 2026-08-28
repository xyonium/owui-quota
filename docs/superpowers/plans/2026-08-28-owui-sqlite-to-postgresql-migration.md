# Open WebUI SQLite → PostgreSQL 迁移与调优计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **注意:** 本计划是运维操作手册而非代码实现,大部分步骤需在宿主机/Portainer 上执行,部分步骤需要人工交互(迁移工具是交互式 CLI)。数据安全优先:全程不修改原 SQLite 文件,任何一步失败都可无损回滚。

**Goal:** 将 Open WebUI 主数据库从 SQLite (`webui.db`, 936MB, 44 表) 无损迁移到 PostgreSQL 16,并应用官方推荐的高性能参数。

**Architecture:** 新建独立 `owui-postgres` 容器 (pgvector/pgvector:pg16,经 `jcr.savorcare.com` 镜像加速) 与 OWUI 同网络;用 OWUI 自身引导空库建表(保证 schema 与 alembic 版本一致),再用社区迁移工具 `open-webui-postgres-migration` v1.0.7(245 stars, PyPI 可装)从 SQLite 只读副本灌数据;切换后应用官方 PG 连接池与调优参数。向量库 (ChromaDB, 859MB) 本次**不迁移**,pgvector extension 先装好为未来铺路。

**Tech Stack:** Docker/Portainer compose、PostgreSQL 16 + pgvector、psycopg3(OWUI 容器内置,无需 asyncpg)、SQLite3 CLI、Python 3.10 宿主机 + pip。

## 现状核查结论(2026-08-28 已实地验证)

| 项目 | 实测值 |
|---|---|
| OWUI 镜像 | `latest-slim` (main-slim, revision `d3e8bf3`),容器名 `open-webui`,stack `open-webui-nogpu` |
| compose 文件 | `/data/compose/230/docker-compose.yml` (Portainer 管理,普通用户不可读) |
| 网络 | `open-webui-nogpu_default` + `reverse-proxy`(外部) |
| SQLite | 936MB,WAL 模式,`PRAGMA quick_check` = **ok**,44 表 |
| 数据量 | chat 1,760 / chat_message 25,116 / chat_file 2,419 / file 2,539 / tag 2,450 / user 66 / auth 66 / api_key 22 / config 418 / model 534 / knowledge 12 |
| 主键类型 | 主表全部为**字符串(UUID)主键** → 无序列修复需求;仅 `config_old`(1行)、`document`(2行)为 INTEGER PK |
| alembic 版本 | SQLite 端 `alembic_version` = `d4c1a8e37b62`(当前镜像 head);`migratehistory` 为 alembic 之前的遗留表 |
| DB 驱动 | 容器内 **asyncpg 未安装**;psycopg3 (3.3.4) + psycopg2 已装。代码注释确认 async engine 用 psycopg v3 → URL 用 `postgresql://` 即可 |
| 密钥 | `/app/backend/.webui_secret_key` 存在于**容器可写层**(33 bytes),不在 data volume 上 → 重建容器会丢,必须备份并固定 |
| 宿主机 | Python 3.10.12 + pip 26.1.2(docker 组);`open-webui-postgres-migration==1.0.7` 在 PyPI 可装,入口命令 `open-webui-migrate` |
| PG 镜像 | `jcr.savorcare.com/docker/pgvector/pgvector:pg16` manifest 验证 **AVAILABLE** |
| 磁盘 | 根分区 90% 已用,剩 ~20G。备份 ~1GB + PG 卷 ~1-2GB,余量够但要留意 |
| Quota Keeper | 使用独立 JSON 文件 + fcntl 锁,**不碰 OWUI 数据库,迁移无需任何改动** |
| ChromaDB | 嵌入式 (chromadb 1.5.9, PersistentClient 跑在 OWUI 进程内),`vector_db/` 859MB = chroma.sqlite3 616MB + HNSW 段文件;**232 个 collection** = 12 知识库 + ~210 `file-*`(聊天/文件 RAG)+ 5 `user-memory-*` + 1 `knowledge-bases`。本次**原地不动**,评估见附录 D |

## 关键风险与对策

1. **迁移工具会 `TRUNCATE TABLE ... CASCADE` 清空目标表** → 目标 PG 必须是刚引导的空库;严格按 Task 顺序执行,灌数据必须在 OWUI 停止状态。
2. **工具按 `sqlite_master` 顺序拷贝,不关闭 FK** → OWUI 表创建顺序恰好父先子后(TRUNCATE CASCADE 父表时子表尚空),顺序天然安全;孤儿行 (`chat_file`/`knowledge_file`) 被工具跳过并计数,其余失败行会打印清单,数据量小可逐行人工处理。
3. **工具跳过 `alembic_version` 和 `migratehistory`** → PG 端版本戳由 OWUI 引导建表时自己 stamp(同一镜像,head 必然一致),这是正确行为,不要手工补。
4. **密钥丢失** → Task 1 备份密钥值,Task 7 固定到 compose env,跨容器重建不再失效(否则所有已登录会话 + 22 个 API key 全部失效)。
5. **停机窗口** → 从 Task 5 停 OWUI 到 Task 7 重新上线,全程约 20-40 分钟,建议凌晨低峰执行。
6. **回滚安全** → 原 `webui.db` 全程只读,迁移源是独立副本;回滚 = 移除 `DATABASE_URL` 重建容器,SQLite 原状恢复。

## 全局约束

- 所有涉及原 `webui.db` 的操作一律只读,任何步骤不得写回原文件。
- 迁移期间 OWUI 必须处于停止状态(Task 5 起),禁止并行写入。
- PG 密码不得出现在任何日志输出中;用 `openssl rand -hex 24` 生成。
- SQLite 备份与 `webui.db` 原文件保留至少 2 周(观察期)后才可清理。
- 数据目录 `open-webui-nogpu_data` 的其他内容(uploads 1.2G、vector_db 859M、quota_keeper)不在本次迁移范围,一个字节都不能动。

---

### Task 1: 迁移前备份(一致性快照 + 密钥)

**Files:** 全部在宿主机 `~/owui-migration/` 目录。

- [ ] **Step 1: 创建工作目录并做 SQLite 一致性快照**(`VACUUM INTO` 生成独立一致副本,在线安全,不锁原库)

```bash
mkdir -p ~/owui-migration && cd ~/owui-migration
docker exec open-webui python3 -c "
import sqlite3
con = sqlite3.connect('/app/backend/data/webui.db')
con.execute(\"VACUUM INTO '/app/backend/data/backup-20260828-webui.db'\")
print('vacuum-into ok')"
docker cp open-webui:/app/backend/data/backup-20260828-webui.db ~/owui-migration/
docker exec open-webui rm /app/backend/data/backup-20260828-webui.db
```

预期: 打印 `vacuum-into ok`;`ls -lh ~/owui-migration/backup-20260828-webui.db` 约 930MB。

- [ ] **Step 2: 校验快照完整性**

```bash
sqlite3 ~/owui-migration/backup-20260828-webui.db "PRAGMA quick_check;"
sqlite3 ~/owui-migration/backup-20260828-webui.db "SELECT count(*) FROM chat_message;"
```

预期: `ok` 和 `25116`(与 Task 6 行数校验的基准一致)。

- [ ] **Step 3: 备份密钥文件并记录值**

```bash
docker cp open-webui:/app/backend/.webui_secret_key ~/owui-migration/webui_secret_key.bak
cat ~/owui-migration/webui_secret_key.bak
```

预期: 输出一段 32 字符左右的随机串。**记下来(或保持文件),Task 7 要用。此值等同管理员签名密钥,不要发给任何人。**

- [ ] **Step 4: 磁盘余量确认**

```bash
df -h / | tail -1
```

预期: `Avail` ≥ 10G。若不足,先清理再继续。

---

### Task 2: 宿主机安装迁移工具

- [ ] **Step 1: 安装**

```bash
python3 -m pip install --user open-webui-postgres-migration==1.0.7
```

- [ ] **Step 2: 验证**

```bash
python3 -m pip show open-webui-postgres-migration | head -3
ls -l ~/.local/bin/open-webui-migrate
```

预期: Version 1.0.7;`~/.local/bin/open-webui-migrate` 存在。若 PATH 不含 `~/.local/bin`,后面用绝对路径调用。

- [ ] **Step 3: 确认宿主 5433 端口空闲**(迁移工具跑在宿主机,连 PG 需经 host 端口映射)

```bash
ss -tln | grep -E ':(5432|5433)\b' || echo "5432/5433 free"
```

预期: 无占用或仅显示其他无关端口。若 5433 被占,Task 3 改用 5434,后续命令同步替换。

---

### Task 3: 部署 PostgreSQL 容器

**Files:** 修改 Portainer stack `open-webui-nogpu` 的 compose 定义(在 Portainer UI: Stacks → open-webui-nogpu → Editor)。

- [ ] **Step 1: 生成密码并记录**

```bash
openssl rand -hex 24 > ~/owui-migration/pg_password.txt && cat ~/owui-migration/pg_password.txt
```

把输出的 48 字符密码记入自己的密码管理器;Task 3/4/6 都要用。此文件用完删除。

- [ ] **Step 2: 在 compose 中新增服务**(与 `open-webui` 服务同级缩进;volumes 段加卷)

```yaml
  owui-postgres:
    image: jcr.savorcare.com/docker/pgvector/pgvector:pg16
    container_name: owui-postgres
    restart: unless-stopped
    environment:
      POSTGRES_USER: qk
      POSTGRES_PASSWORD: <上一步生成的密码>
      POSTGRES_DB: openwebui
      PGDATA: /var/lib/postgresql/data/pgdata
    command: >-
      postgres
      -c shared_buffers=2GB
      -c effective_cache_size=6GB
      -c max_connections=200
      -c work_mem=16MB
      -c maintenance_work_mem=512MB
      -c wal_buffers=64MB
      -c checkpoint_completion_target=0.9
      -c random_page_cost=1.1
    volumes:
      - owui-postgres-data:/var/lib/postgresql/data
    ports:
      - "127.0.0.1:5433:5432"
    # 不写 networks: 让 compose 自动加入本 stack 的默认网络
    # (即 open-webui 所在的 open-webui-nogpu_default),通过 container_name 提供 DNS 名
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U qk -d openwebui"]
      interval: 10s
      timeout: 5s
      retries: 5
```

并在文件末尾 `volumes:` 段追加:

```yaml
  owui-postgres-data:
```

在 Portainer 中点 **Update the stack**(re-pull 由 daemon 自动走镜像加速)。此时 OWUI 无需停止,新服务独立启动。

- [ ] **Step 3: 验证 PG 就绪**

```bash
docker ps --format '{{.Names}}\t{{.Status}}' | grep owui-postgres
docker exec -i owui-postgres psql -U qk -d openwebui -c 'SELECT version();'
```

预期: `Up ... (healthy)`;version 输出含 `PostgreSQL 16.x`。若 unhealthy,`docker logs owui-postgres` 查看(常见原因:端口冲突、PGDATA 目录权限,新卷不会有)。

- [ ] **Step 4: 确认与 open-webui 同网络(否则 Task 5 的 `owui-postgres` 主机名解析不到)**

```bash
docker inspect open-webui --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'
docker inspect owui-postgres --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'
```

预期: 两者共享 `open-webui-nogpu_default`(名字相同)。若不同,在 compose 里给 owui-postgres 显式加上该网络再更新 stack。

---

### Task 4: 启用 pgvector extension(为未来向量库迁移铺路,本次不切换向量库)

- [ ] **Step 1: 创建 extension**

```bash
docker exec -i owui-postgres psql -U qk -d openwebui -c 'CREATE EXTENSION IF NOT EXISTS vector;'
docker exec -i owui-postgres psql -U qk -d openwebui -c '\dx'
```

预期: 列表中出现 `vector`。注意 **不要**给 OWUI 设置 `VECTOR_DB=pgvector` —— 现有 ChromaDB 有 232 个 collection(12 知识库 + ~210 文件 RAG + 5 用户记忆),一旦切换 OWUI 会连到空的 pgvector 集合,所有知识库检索、文件 RAG、个人记忆静默失效。本次保持 ChromaDB 不变,评估见附录 D。

---

### Task 5: 引导 PG 空库建表(OWUI 短暂切换,停机窗口开始)

- [ ] **Step 1: 通知用户并停止 OWUI**

```bash
docker stop open-webui && docker ps --format '{{.Names}}' | grep -c open-webui || true
```

预期: 停止成功(输出 0 表示容器已不在运行列表)。**从此刻起 OWUI 对外不可用,后续步骤按顺序快速执行。**

- [ ] **Step 2: 在 compose 中给 open-webui 服务添加 DATABASE_URL**(此时只加这一项,其他 env 变更留到 Task 7)。同时显式设置 `ENABLE_DB_MIGRATIONS=true`(单实例默认即为 true,显式写出确保容器以迁移模式启动)

```yaml
      - DATABASE_URL=postgresql://qk:<密码>@owui-postgres:5432/openwebui
      - ENABLE_DB_MIGRATIONS=true
```

Portainer 更新 stack。注意 URL 是 `postgresql://`(psycopg3 驱动,容器内置;不要用文档老版写法 `postgresql+asyncpg://`,asyncpg 未安装)。

- [ ] **Step 3: 启动并确认建表**(容器停止状态下 `docker start` 即可,Portainer 更新 stack 后若容器已被自动重建为 Up 状态,则直接看日志)

```bash
docker start open-webui 2>/dev/null || true
sleep 30
docker logs --since 2m open-webui 2>&1 | grep -iE 'PostgresqlImpl|alembic|Running upgrade' | head -5
```

预期: 日志出现 `Context impl PostgresqlImpl`(而不是 SQLiteImpl),可能伴随多条 `Running upgrade`。

- [ ] **Step 4: 验证 PG 端 schema 与版本戳**

```bash
docker exec -i owui-postgres psql -U qk -d openwebui -c '\dt' | head -30
docker exec -i owui-postgres psql -U qk -d openwebui -c 'SELECT * FROM alembic_version;'
```

预期: 列出 `user / chat / chat_message / ...` 全部表;`alembic_version` = `d4c1a8e37b62`(与 SQLite 端一致,已实测 heads=current=该值)。

- [ ] **Step 5: 再次停止 OWUI**

```bash
docker stop open-webui
```

预期: 停止成功。此时 PG 里只有 OWUI 引导时写入的种子数据,Task 6 将被全部 TRUNCATE 后重灌,无损失。

---

### Task 6: 数据迁移与校验

- [ ] **Step 1: 准备只读迁移源(用 Task 1 快照副本,绝不用原文件)**

```bash
cd ~/owui-migration
cp backup-20260828-webui.db migrate-src.db
sqlite3 migrate-src.db "PRAGMA quick_check;"
```

预期: `ok`。

- [ ] **Step 2: 运行迁移工具(交互式,必须在终端上由你本人操作)**

```bash
~/.local/bin/open-webui-migrate
```

按提示依次输入:

| 提示项 | 输入 |
|---|---|
| SQLite database path | `/home/eli/owui-migration/migrate-src.db` |
| PostgreSQL host | `127.0.0.1` |
| port | `5433` |
| database | `openwebui` |
| user | `qk` |
| password | Task 3 生成的密码 |
| batch size | 500(默认) |

预期流程: 完整性检查(quick_check / FK check,可能报告 `chat_file`/`knowledge_file` 孤儿行 → 属预期,工具会跳过)→ 逐表 TRUNCATE + 批量插入 + 进度条 → 结束打印每表迁移行数与 failed rows 清单。总耗时预计 5-15 分钟(总行数约 3.4 万)。

- [ ] **Step 3: failed rows 处理**

查看工具结尾输出: 若 `Failed rows` 为 0(或只有 FK 跳过计数),直接进入下一步。若 >0,记录 `(表名, 行号, 错误)`,绝大多数情况是孤儿引用,与本行数校验的差额对照确认后继续(该行在 SQLite 里也存在,只是引用断裂,不影响正常使用)。

- [ ] **Step 4: 行数对比校验**(写一个对比脚本)

```bash
cd ~/owui-migration
cat > verify.sh <<'EOF'
#!/bin/bash
# 生成两边行数清单并 diff
TABLES="user auth api_key chat chat_message chat_file file folder tag config model function tool prompt shared_chat memory group group_member channel channel_member feedback note knowledge knowledge_file"
for t in $TABLES; do
  sqlite3 migrate-src.db "SELECT '$t', count(*) FROM \"$t\";"
done | sort > sqlite_counts.txt
for t in $TABLES; do
  docker exec -i owui-postgres psql -U qk -d openwebui -tA -c "SELECT '$t', count(*) FROM \"$t\";"
done | sort > pg_counts.txt
diff sqlite_counts.txt pg_counts.txt && echo "=== ALL TABLE COUNTS MATCH ===" || echo "=== DIFF FOUND, REVIEW ABOVE ==="
EOF
bash verify.sh
```

预期: `=== ALL TABLE COUNTS MATCH ===`。若有差异,差异表即迁移工具报告的 failed rows 来源,逐表复查。

- [ ] **Step 5: 修复遗留 INTEGER 序列**(仅 `config_old`/`document` 两表,防未来插入撞主键)

```bash
docker exec -i owui-postgres psql -U qk -d openwebui -c "SELECT setval(pg_get_serial_sequence('config_old','id'), (SELECT COALESCE(MAX(id),1) FROM config_old));"
docker exec -i owui-postgres psql -U qk -d openwebui -c "SELECT setval(pg_get_serial_sequence('document','id'), (SELECT COALESCE(MAX(id),1) FROM document));"
```

预期: 输出 setval 结果。若 `pg_get_serial_sequence` 返回空(说明 PG 端无序列),则该表 PG 端是 int 非 serial,跳过即可,无需处理。

- [ ] **Step 6: 关键数据抽查**

```bash
docker exec -i owui-postgres psql -U qk -d openwebui -c "SELECT id, email, role FROM \"user\" LIMIT 5;"
docker exec -i owui-postgres psql -U qk -d openwebui -c "SELECT count(*) FROM auth;"
docker exec -i owui-postgres psql -U qk -d openwebui -c "SELECT count(*) FROM chat;"
```

预期: 用户列表含你自己的账号且 role=admin;auth=66;chat=1760。看一眼邮箱/角色无乱码(UTF-8 正常)。

---

### Task 7: 正式切换(env 变更 + 密钥固定,停机窗口结束)

**Files:** Portainer stack compose。

- [ ] **Step 1: 一次性更新 open-webui 服务的全部数据库相关 env**(对照下表,精确操作)

| 变量 | 动作 | 新值 |
|---|---|---|
| `DATABASE_URL` | 保留(Task 5 已加) | `postgresql://qk:<密码>@owui-postgres:5432/openwebui` |
| `DATABASE_SQLITE_PRAGMA_CACHE_SIZE` | **删除** | — |
| `DATABASE_SQLITE_PRAGMA_MMAP_SIZE` | **删除** | — |
| `DATABASE_POOL_SIZE` | 改为 | `15`(官方 PG 起点) |
| `DATABASE_POOL_MAX_OVERFLOW` | 新增 | `20` |
| `DATABASE_ENABLE_SESSION_SHARING` | 新增 | `True`(官方: PG + 资源充足时建议开启) |
| `DATABASE_USER_ACTIVE_STATUS_UPDATE_INTERVAL` | 新增 | `120`(官方全部署建议,减半 presence 写入) |
| `WEBUI_SECRET_KEY` | 新增 | Task 1 Step 3 备份的密钥值 |
| `THREAD_POOL_SIZE=2000` | 保留不动 | — |
| `ENABLE_DB_MIGRATIONS` | 可选显式写 | `true`(单实例默认即为 true) |

Portainer 更新 stack(会重建 open-webui 容器;`owui-postgres` 不受影响)。

- [ ] **Step 2: 启动健康检查**

```bash
sleep 30
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'open-webui|owui-postgres'
docker logs --since 3m open-webui 2>&1 | grep -iE 'PostgresqlImpl|error|traceback' | head -10
```

预期: 两者 healthy/Up;出现 `Context impl PostgresqlImpl`;无 error/traceback。

- [ ] **Step 3: 确认 SQLite 已停止被写**

```bash
stat -c '%y %n' /var/lib/docker/volumes/open-webui-nogpu_data/_data/webui.db; sleep 60; stat -c '%y %n' /var/lib/docker/volumes/open-webui-nogpu_data/_data/webui.db
```

预期: 两次 mtime 相同(若此命令无权限读 volume,用 `docker exec open-webui stat -c '%y' /app/backend/data/webui.db` 替代)。

- [ ] **Step 4: 清理工作文件(保留备份)**

```bash
rm -f ~/owui-migration/migrate-src.db ~/owui-migration/sqlite_counts.txt ~/owui-migration/pg_counts.txt ~/owui-migration/pg_password.txt
```

预期: 保留 `backup-20260828-webui.db` 与 `webui_secret_key.bak`(至少 2 周)。

---

### Task 8: 上线验证清单

逐项勾选,任一项失败立即看 Task 9 回滚:

- [ ] 浏览器登录 OWUI(自己账号),观察历史对话列表(验证 `chat`/`chat_message`)
- [ ] 打开一个历史对话继续发消息(验证流式 + 写入 PG)
- [ ] 管理面板 → Users 列出 66 个用户,角色正确(验证 `user`/`auth`)
- [ ] Models 页显示 534 个模型,发起一次真实上游对话(此前 499 场景复测)
- [ ] Files 页文件列表完整,下载一个旧文件(验证 `file` 记录 + uploads 卷未被触碰)
- [ ] Knowledge 12 个知识库可打开、可检索(验证知识库 + ChromaDB 未受影响)
- [ ] Workspace → Functions 里 quota_keeper 两个函数正常,`/quota` 页面打开(Quota Keeper 独立 JSON,应完全无感)
- [ ] 用已有 API key 调一次接口: `curl -H "Authorization: Bearer <某API_KEY>" http://127.0.0.1:8080/api/models`(验证 22 个 key 兼容性)
- [ ] 换一个普通用户账号登录,历史与头像正常
- [ ] PG 连接观察: `docker exec -i owui-postgres psql -U qk -d openwebui -c "SELECT state, count(*) FROM pg_stat_activity GROUP BY state;"` → idle 为主,无排队堆积

---

### Task 9: 观察期监控与回滚手册

**监控(上线后 48h 内每天看一次):**

```bash
docker logs --since 24h open-webui 2>&1 | grep -icE 'queuepool|timeouterror|connection timed out' 
docker exec -i owui-postgres psql -U qk -d openwebui -c "SELECT state, count(*) FROM pg_stat_activity GROUP BY state;"
```

预期: 第一条输出 0(再没有 QueuePool 饥饿);第二条无大量 `active` 堆积。

**性能调优(观察后如需更强):**
- 池上调: `DATABASE_POOL_SIZE=25` + `DATABASE_POOL_MAX_OVERFLOW=25`(单实例上限合计 ≤50,官方要求远低于 PG `max_connections`,已配 200,余量充足)。
- PG 侧 `shared_buffers` 可从 2GB 逐步上探(当前已按 58GB 内存的保守档配置)。

**回滚(观察期内任意时刻):**
1. Portainer 中移除 `DATABASE_URL` 及 Task 7 新增项,恢复 SQLite 默认 → 更新 stack。
2. 原 `webui.db` 从未被改动,OWUI 重启即回到迁移前状态。
3. **注意**: PG 运行期间产生的新对话/配置在 SQLite 里没有——若观察期超过半天才回滚,期间数据需人工取舍(或重跑 Task 5-7 再迁一次,增量会重灌)。
4. `owui-postgres` 容器与卷保留,回滚后再迁时直接复用。

---

## 附录 A: env 对照总表(SQLite 现状 → PG 终态)

| 变量 | 迁移前 | 迁移后 | 依据 |
|---|---|---|---|
| `DATABASE_URL` | (默认 SQLite) | `postgresql://qk:***@owui-postgres:5432/openwebui` | psycopg3 内置 |
| `DATABASE_POOL_SIZE` | 50 | 15(可升至 25) | 官方 scaling 文档起点 |
| `DATABASE_POOL_MAX_OVERFLOW` | 未设 | 20(可升至 25) | 同上 |
| `DATABASE_SQLITE_PRAGMA_CACHE_SIZE` | -2000 | 删除 | SQLite 专属 |
| `DATABASE_SQLITE_PRAGMA_MMAP_SIZE` | 0 | 删除 | SQLite 专属 |
| `DATABASE_ENABLE_SESSION_SHARING` | 未设 | True | 官方: PG+资源充足建议开 |
| `DATABASE_USER_ACTIVE_STATUS_UPDATE_INTERVAL` | 未设(60) | 120 | 官方 performance 文档 |
| `WEBUI_SECRET_KEY` | 空(容器层文件) | 固定值 | 防重建丢会话/API key |
| `THREAD_POOL_SIZE` | 2000 | 2000 | 官方 scaled 推荐,保留 |

## 附录 B: PG 容器调优参数说明(58GB RAM / 20 核宿主机,保守档)

| 参数 | 值 | 说明 |
|---|---|---|
| `shared_buffers` | 2GB | 热数据缓存;宿主机还有其他容器,留足余量,可上探 4GB |
| `effective_cache_size` | 6GB | 查询规划器对 OS 页缓存的估计 |
| `max_connections` | 200 | OWUI 单实例 pool 合计 ≤50,余量充足 |
| `work_mem` | 16MB | 排序/哈希单操作内存,20 核下够用 |
| `maintenance_work_mem` | 512MB | VACUUM/索引重建 |
| `wal_buffers` | 64MB | 写吞吐 |
| `checkpoint_completion_target` | 0.9 | 平滑写盘,避免 IO 尖峰 |
| `random_page_cost` | 1.1 | 本地 SSD 取值 |

## 附录 D: ChromaDB 处置评估(2026-08-28 实测)

**现状**:OWUI 单实例 `UVICORN_WORKERS=1`,ChromaDB 以嵌入式 `PersistentClient` 跑在 OWUI 进程内,数据在 `open-webui-nogpu_data/vector_db/`(859MB,其中 chroma.sqlite3 616MB)。232 个 collection 构成:12 个知识库(名称与 `knowledge` 表一一对应)、~210 个 `file-*`(聊天/文件 RAG 嵌入)、5 个 `user-memory-*`(个人记忆)、1 个 `knowledge-bases`。

**决策:本次迁移不碰 ChromaDB。** 理由:

1. 官方 Scaling 文档明确:嵌入式 ChromaDB 对**单 worker 单实例**是安全默认,只有 `UVICORN_WORKERS > 1` 或多副本才会触发 fork 崩溃/损坏风险 —— 本实例不满足触发条件。
2. 本次故障(QueuePool 饥饿 / 499)全部指向主库,与 ChromaDB 无关。
3. 迁移主库时 `vector_db` 卷原样保留,OWUI 重建容器后继续使用,零风险。
4. 切换 `VECTOR_DB=pgvector` 没有官方数据迁移工具,232 个集合会全部静默失效 —— 收益为负。

**何时需要迁 pgvector(二期触发条件)**:

- 未来把 OWUI 扩到多 worker / 多副本(官方硬性要求,SQLite 版 Chroma 会直接崩溃/损坏);
- RAG 检索成为性能瓶颈,或 ChromaDB 数据损坏。

**二期迁移路径(满足触发条件后执行,不在本次范围)**:

1. 前提已铺好:本计划 Task 4 已装 `vector` extension,PG 卷有空间。
2. 给 OWUI 设置 `VECTOR_DB=pgvector` + `PGVECTOR_DB_URL=postgresql://qk:***@owui-postgres:5432/openwebui`。
3. 旧向量无自动迁移:12 个知识库需**删除文档重新导入**重建(原文仍在 `uploads/` 与 `knowledge_file` 记录中,工作量可控);`file-*` 与 `user-memory-*` 会失效并随时间自然重建 —— 聊天内容本身在 PG 主库里不受影响,丢失的只是"新对话里对这些历史文件做 RAG 检索"的上下文。
4. 验证通过前保留 ChromaDB 数据;通过后确认 OK 再清 `vector_db/`。

**观察项**:chroma.sqlite3 目前 616MB 且随文件上传增长。根分区仅余 20GB,若未来告急,优先清理过期 `file-*` collection(可通过 OWUI 界面删旧文件)而非急着迁移。

## 附录 C: 参考资料

- 官方迁移手册: https://docs.openwebui.com/troubleshooting/manual-database-migration/
- 官方 Scaling(Step 1 即 PG 指引): https://docs.openwebui.com/getting-started/advanced-topics/scaling/
- 官方 Performance & RAM(数据库优化章节): https://docs.openwebui.com/troubleshooting/performance
- 迁移工具: https://github.com/taylorwilsdon/open-webui-postgres-migration (v1.0.7, PyPI `open-webui-postgres-migration`)
