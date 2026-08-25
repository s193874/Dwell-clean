# 项目交接：resident 入住、Dwell 架构与运行态

这份文档是当前项目的现役交接入口：先看这里，再看功能专题。
它覆盖 resident（驻客）入住 ChatGPT、Dwell、`sibylsea-hub/gpt-thinking-block-mcp`，
以及聊天、API、日报和表情包的设计。私有 MCP 完整地址、token、API token 和个人数据
都不属于文档内容，也不会出现在仓库里。

> 这是从一个私人实例剥离出来的干净版本。文档里所有的路径、端口、PID、备份目录都是
> 示例占位；接手时用你自己环境的真实值替换。`resident` / 驻客是入住 AI 的中性默认名，
> 可通过 `DWELL_MCP_RESIDENT_NAME` 配置成任何显示名。

## 运行态骨架

- 代码根目录：你的 Dwell 检出路径（下文记作 `<checkout>`）；默认入口是
  `python3 -m backend.app`，本地默认监听 `127.0.0.1:8765`。核心后端只用 Python 标准库和
  SQLite；`requirements-search.txt` 的 `sqlite-vec` 只是混合历史检索的可选加速，缺失时使用
  Python cosine 回退。运行时数据放在 `DWELL_DATA_DIR`（下文记作 `<data-dir>`），不在仓库里。
- 生产 Dwell 建议由 `deploy/dwell.service` 管理。`deploy/` 下的是模板，复制到
  `/etc/systemd/system/` 后必须把每个 `YOUR-*` / `/path/to` 占位换成你的真实值再启用。
- 思考 MCP 由 `dwell-thinking.service` 管理，运行同级的
  `gpt-thinking-block-mcp`；它的端口要和 `dwell.service` 里的 `THINKING_MCP_URL` 一致。
- PID、端口、systemd 状态都是运行时值，接手时用 `systemctl show` / `curl` 重新读取，
  不要把文档里的示例值当配置。

### 接手时最短路径

```bash
cd <checkout>
python3 -m py_compile backend/*.py
python3 -m unittest discover -s tests -v
systemctl is-active dwell.service   # 如果你用 systemd 部署
curl -fsS http://127.0.0.1:8765/api/status
```

生产重启、删除语义、数据库迁移都受仓库及上级 `AGENTS.md` 约束；没有对本次独立动作的
明确授权，不要执行。

## 当前已实现的用户功能

- **聊天窗口**：最近列表支持改名、删除；删除会话会级联删除该会话消息。消息下方有复制、
  刷新、编辑按钮；刷新会重新调用模型。长按自己的消息有复制、编辑、编辑后重新发送。
- **原地编辑**：编辑会把原气泡变成文本框；保存通过 `/api/message-action` 的 `edit` 更新
  同一条消息，不复制成新消息、不自动发送。`edit_resend` 才会替换用户消息之后的分支并重新回答。
- **API 接入**：保存多个 OpenAI-compatible API profile，可切换/删除；令牌不从读取接口返回；
  模型在单独的“选择模型”入口设置，也支持自定义模型。旧的单 API 设置会在内存中迁移为
  `legacy` profile，保存后才持久化为新结构。
- **表情包**：主聊天输入区有独立笑脸入口，在原输入框内展开真实搜索网格；
  用户点击后只向后端交付 `sticker_id`。主模型也可按语境先搜索再发一张，但 ID
  必须来自本轮真实候选，且每轮最多一张。浏览器不接触 MCP 地址或其他服务端配置。
  需要配置 `DWELL_STICKER_MCP_URL`；留空则禁用。
- **日报**：`tools/news_daily.py` 支持四个版块、RSS/Atom 素材抓取、`--collect-only`、
  `--dry-run` 和按日期原子写入 `news/YYYY-MM-DD.md`。可配 `dwell-news-daily.timer` 定时运行；
  完整生成需要 `DWELL_API_BASE`、`DWELL_API_TOKEN`、`DWELL_MODEL` 或数据库里的同等配置。
- 日历、日记墙和日报的空数据/失败态已补齐；具体数据结构仍以当前 `backend/`、SQLite schema
  和专题文档为准。
- **用户/resident 对齐**：附件以同一结构出现在实时等待和历史读取中；图片可返回 MCP image
  block，文本附件可按 id 读取。表情、主动消息、段内引用、待办、日历、双作者日记、日报点评、
  当天聚合上下文都已有 resident 工具出口。网页日记只能写/删 owner 条目，resident MCP 只能
  写/改/删 resident 条目。
- **历史检索**：`search_chat_history` 支持 keyword/semantic/hybrid、speaker 与日期过滤；语义索引
  包含单条消息和连续 3/6 turn 小段，融合结果只给 snippet，再由 `read_message_context` 按需展开。
  新用户消息的 `related_history` 只在阈值以上给最多三条摘要，并排除最近三分钟。
  所有者可在现有「API 接入」设置页另填 OpenAI-compatible embeddings 中转的地址、token 和模型，
  用合成文本测试后保存；token 不从读取接口回传，保存后 history service 立即切换而无需重启。
  `DWELL_EMBEDDING_*` 环境变量仍可用于服务器托管配置，并优先于页面保存值。
- **统一业务事件**：消息、共读、待办、日历、日记和日报写入 durable `domain_events`；resident
  用 `read_dwell_events(after_event_id, types?)` 按游标读取。实时 UI 的短期 `events` 表仍独立，
  本版没有声称实现一个会永久唤醒模型的后台 worker。

## resident 入住机制

- ChatGPT 当前活跃回合通过入住 MCP 收到用户消息。
- `send_dwell_reply` 必须提交 `reply_to_seq`、最终 `text`，以及上游合同的
  `style`、`thinking`、`effort`、`skin` 四个字段。
- Dwell 用 `ThinkingBridge` 调用现役 `render_thinking_block`。
- 只有调用成功，数据库才按 `think → gu` 保存思考与最终回复，并发送现有前端事件。
- 缺字段、非法枚举或 thinking MCP 不可用时，工具返回错误且不保存最终回复，由当前回合重试。

入住 MCP 不再用容易过期的“工具数量”描述能力；以实际 `tools/list` 为准。当前工具覆盖进入/读取/
等待/回复/主动消息、附件、表情、共读记事、四个生活模块与当天上下文、混合历史检索、按需上下文
和 durable 业务事件读取。`send_dwell_message` 的 `reply_to_seq` 可空，`client_message_id` 用于重试
幂等；带 quote 时可精确到 `start_offset/end_offset`。
`send_dwell_reply_and_wait` 先复用现有回复、thinking MCP 校验和 `reply_to_seq` 幂等逻辑，
再在同一次工具调用里等待下一批消息，避免“回复发出后 resident 顺手结束”的空档。

`wait_for_user_message` 默认使用 `continuous=true`：服务端会保持当前 MCP HTTP 请求，
通过 SSE 注释发送心跳，直到用户消息、共读事件或重新回答请求到达才返回。只有明确传入
`continuous=false` 时，才按 `timeout_seconds` 做有限等待。每次等待都会记录 request id、
起止时间、游标、超时和断开原因（`mcp_wait_log` 表）。

MCP 连接不会在服务器上复制出一个 24×7 的 ChatGPT 进程。真正思考和调用工具的仍是
ChatGPT 当前活跃的对话回合；该回合结束后，服务器只会继续保存和排队 Dwell 消息，不会
凭空让模型在后台永久运行。

## 历史隔离（重要）

- Dwell 用户端的 `/api/messages` 继续返回 `think`，用于当前界面显示。
- 入住端的 `enter_dwell.recent_messages` 和 `read_dwell_messages.messages` 只返回
  `me`、`gu`、`nook`，不会返回数据库里的 `think` 行。
- `wait_for_user_message` 原本就只等待 `me` 与 `nook`。
- 这保证 Dwell 不会在 resident 主动读取历史时把可见工作摘要重新回灌。ChatGPT 宿主是否保留
  模型自己刚提交过的工具参数属于宿主上下文行为，不由 Dwell 的历史接口控制。

## capability URL 安全模型

当前接法不使用 OAuth。后端首次启动会在 `DWELL_MCP_TOKEN_FILE` 生成权限为 `0600` 的
随机连接钥匙。可信反向代理只应公开形如 `/dwell-mcp/<long-random-token>/mcp` 的路径，
不能把同一前缀下的 `/api/*` 一并代理。配置 `DWELL_MCP_PUBLIC_BASE` 后，Dwell 所有者可从
登录保护的 `/mcp-link` 页面复制完整连接地址，也可在那里轮换钥匙；轮换后旧地址立即失效。

这条专属地址本身就是权限，不是 ChatGPT 账号身份认证：任何拿到地址的人都能读取和回复
Dwell。不要把它写进日志、聊天、源码或公开文档；代理访问日志应保持关闭，后端会把匹配
到的连接路径打码。`DWELL_MCP_OWNER_CHECK_URL` 与 `DWELL_MCP_OWNER_USER_ID` 用于确保
只有指定的现有登录账号能打开连接页。

## 表情包链路

1. 人工入口请求 `/api/stickers`，后端代理真实 MCP 搜索并解析可显示的公开媒体；
   浏览器只收到候选和图片地址。
2. 点击候选只向 `/api/send` 交付 `sticker_id`；后端重新解析该 ID，并把用户可见图片、
   语义描述和视觉输入交给当前主模型。
3. 主模型使用 `search_stickers` / `send_sticker` 时，Dwell 只接受本轮真实搜索返回的 ID，
   并在后端强制一轮最多一张，避免编造 ID 或循环发送。
4. `DWELL_STICKER_MCP_URL` 配置服务端地址；留空时表情功能禁用。

## 共读机制

共读正文按页显示；页面首次交付按稳定的 `reading:{slug}:{chapter}:{page}` key 向 resident 窗口
排入一个隐藏共读事件。单纯 reopen/reconnect 不重发；换页、换章节、换书或显式 force 才新增交付。
事件带当前页、上一页、底层固定的章节末更新规则和只含标题/摘要的记事目录。章节最后一页
会提醒 resident 按规则更新记事。阅读页右下角的半透明入口只负责打开“聊天 / 记事本”双页小窗，
不控制是否共享；小窗可明确收起。小窗聊天直接复用主聊天的 `.bubble` / `.gu` 样式。
记事本首页只列标题，每行是只有底边线的框并复用章节目录选中态；点标题才显示摘要、正文与操作；新建
表单由小图标点开。每本书的记事支持用户与 resident 读写、删除和置顶。resident 检索只返回标题和摘要，
选定一条后才能单独读取标题、摘要和正文。书架删除整本书时会二次确认，并一并删除其阅读进度、批注、
记事本和内部设置。自动事件仍需 ChatGPT 当前回合持续调用等待工具才能被即时处理；回合结束后事件会保留到下次读取。

## 关键文件

- `backend/app.py`：同一 `ThinkingBridge` 同时注入 API 对话和入住 MCP。
- `backend/resident_mcp.py`：工具 schema、严格调用顺序、历史过滤和事件落库。
- `backend/thinking_bridge.py`：读取上游 schema，并调用 `render_thinking_block`。
- `backend/sticker_bridge.py`：表情 MCP 工具发现、真实搜索、ID/媒体约束和人工选择器数据。
- `backend/daily_report.py`：网页与 MCP 共用的日报正文/点评解析出口。
- `backend/history_search.py`：网页搜索、MCP 检索和被动相关历史共用的关键词/向量/RRF 出口。
- `backend/store.py`：SQLite schema 和持久化（含 `mcp_replies`、`mcp_wait_log`、
  `nook_*` 等表）。
- `web/index.html`：现有 `Thought process` 渲染、聊天按钮、长按菜单、原地编辑和表情选择 UI。
- `tests/test_backend.py`：思考链、历史隔离、模型表情搜索/发送和人工表情发送的集成覆盖。
- `tests/test_sticker_runtime.js`：不启动浏览器的表情入口、搜索和点击发送交互验证。
- `tests/test_message_runtime.js` / `test_calendar_runtime.js` / `test_nook_runtime.js` /
  `test_news_runtime.js`：不启动浏览器的消息去重、引用、日历/经期、共读和日报渲染验证。

## 修改的验证与发布门

```bash
python3 -m py_compile backend/*.py
python3 -m unittest discover -s tests -v
node tests/test_sticker_runtime.js
node tests/test_message_runtime.js
node tests/test_calendar_runtime.js
node tests/test_nook_runtime.js
node tests/test_news_runtime.js
```

上述命令在 2C2G 主机上必须逐项串行执行。语义检索还应在实际 embeddings provider 下做一次
集成调用；若安装了可选依赖，再用 `PYTHONPATH=<sqlite-vec-dir>` 验证 native 距离路径。

任何改动都要先备份目标文件和在线 SQLite；部署后核对 Dwell 状态、thinking
健康、入住工具 schema、真实 HTTP 前端和错误日志，并让 ChatGPT 重新连接或刷新工具 schema
后再做实际回复验收。
