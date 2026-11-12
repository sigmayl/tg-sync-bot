# tg-sync

Telegram **机器人**：在聊天里发 `/sync <url>`，机器人把链接里的媒体下载到本地，
再作为**对这条命令的回复**上传回来。

```
你:      /sync https://x.com/NASA/status/2119083432835764635
机器人:  🆔 a1b2c3d4 · ⏳ 排队中        [⏸ 暂停] [✖ 取消]
机器人:  🆔 a1b2c3d4 · ⬇️ 下载中：X 推文 2119083432835764635
机器人:  🆔 a1b2c3d4 · ⬆️ 上传中：45%
机器人:  ↩️ 回复命令：2119083432835764635_01.mp4（4K，112 MiB）
```

任务状态存在本地 SQLite（`state/tasks.db`），**进程重启后未完成的任务会自动继续**；
状态消息上的按钮可以随时暂停 / 取消 / 继续。

## 上传为什么能超过 50 MiB

Bot API 的 HTTP 接口（`api.telegram.org`）限制机器人上传 50 MiB，只有自建
**Bot API Server** 才能到 2000 MiB。自建 server 内部其实就是用 MTProto 的
`upload.saveBigFilePart` 分片上传。

本项目**不启动 Bot API Server**，而是用 Telethon 直接以 bot token 登录 MTProto，
`send_file` 会自动走 `upload.saveBigFilePart`（>10 MiB 时，512 KiB 分片），
因此同样能上传到 **2000 MiB**。消息收发也全部走 MTProto，不需要任何本地服务。

## 支持的链接

| 类型 | 示例 | 说明 |
| --- | --- | --- |
| X / Twitter 推文 | `https://x.com/<user>/status/<id>` | 图片按原图下载；视频取最高码率 mp4（含 4K） |
| X 短链域名 | `fxtwitter.com` / `vxtwitter.com` / `fixupx.com` | 自动归一化到 x.com |
| t.me 公开消息 | `https://t.me/<channel>/<msg_id>` | 机器人需要能访问该频道 |
| t.me 私有消息 | `https://t.me/c/<internal_id>/<msg_id>` | 机器人需要是该频道成员/管理员 |

- 一条命令可带多个链接（空格/换行分隔，最多 10 个）。
- t.me 相册（grouped_id）会把同组消息一起下载。
- 超过 10 个文件自动分成多个相册上传。
- 其他站点会明确回复「暂不支持」。

> 机器人读不到你的「收藏夹」（Saved Messages 只属于账号本人）。所以默认结果是
> **回复命令所在聊天**；想把结果集中到某个频道，设置 `TG_TARGET_CHAT`。
>
> 想要接近「收藏夹」的体验：建一个只有自己的私有频道，把机器人设为管理员，
> 然后在频道里发 `/id` 拿到 `-100…` 形式的 chat_id，填进 `TG_TARGET_CHAT`。

## 安装

需要 Python ≥ 3.10。**建议安装 `ffmpeg`/`ffprobe`**：用于读取视频时长/分辨率并抽封面，
以及 X 视频需要 yt-dlp 兜底时的合并。没有 ffmpeg 也能跑，但视频会没有封面、不显示时长。

```bash
git clone <this-repo> tg-sync-tool
cd tg-sync-tool
python3 -m venv .venv
.venv/bin/pip install -e .

# 使用 socks5 代理时
.venv/bin/pip install -e ".[socks]"
```

## 配置

1. 找 [@BotFather](https://t.me/BotFather) 创建机器人，拿到 **bot token**。
2. 打开 <https://my.telegram.org/apps> 创建应用，拿到 **api_id / api_hash**
   （MTProto 登录和 2000 MiB 上传需要）。
3. 复制配置模板并填写：

```bash
cp .env.example .env
$EDITOR .env
```

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TG_BOT_TOKEN` | 必填 | @BotFather 给的 token |
| `TG_API_ID` | 必填 | my.telegram.org 的 api_id |
| `TG_API_HASH` | 必填 | my.telegram.org 的 api_hash |
| `TG_SESSION` | `tg-sync-bot` | session 文件名 |
| `TG_ALLOWED_USERS` | 空 | 允许使用的 user id（逗号/空格分隔）。**空 = 拒绝所有人** |
| `TG_TARGET_CHAT` | 空 | 结果发到 `-100…` / `@channel`；空 = 回复命令所在聊天 |
| `TG_DOWNLOAD_DIR` | `downloads` | 下载临时目录（上传成功后删除） |
| `TG_TASK_DB` | `state/tasks.db` | 任务队列（SQLite），重启后恢复 |
| `TG_MAX_FILE_SIZE_MB` | `2048` | 上传上限（机器人上限 2000） |
| `TG_STATUS_MESSAGES` | `true` | 是否显示进度消息（成功后自动删除） |
| `TG_KEEP_FILES` | `false` | 下载后保留本地文件 |
| `TG_PROXY` | 空 | 例如 `socks5://127.0.0.1:1080`、`http://127.0.0.1:7890` |
| `TG_YTDLP_COOKIES` | 空 | X 需要登录时用的 cookies.txt（Netscape 格式） |
| `TG_YTDLP_COOKIES_FROM_BROWSER` | 空 | 或直接从浏览器读，如 `chrome`、`firefox` |
| `TG_ALBUM_LIMIT` | `10` | 每个相册的文件数（1–10） |
| `TG_QUEUE_SIZE` | `8` | 待处理命令队列长度 |
| `TG_JOB_TIMEOUT` | `3600` | 单个任务超时（秒），0 = 不限 |
| `TG_WORKERS` | `3` | 并发处理的任务数（1–8） |
| `TG_SHUTDOWN_GRACE` | `300` | 退出时等待进行中任务完成的秒数 |
| `TG_LOG_LEVEL` | `INFO` | 日志级别 |

## 首次登录与授权

```bash
.venv/bin/tg-sync login     # 用 bot token 登录，生成 tg-sync-bot.session
.venv/bin/tg-sync run       # 启动机器人
```

然后在 Telegram 里给机器人发 `/start`：

- 如果 `TG_ALLOWED_USERS` 为空，机器人会回复你的 **user id**，
  把它填进 `.env` 后重启即可。
- 填好后，`/id` 可以随时查看当前 `chat_id` / `user_id`（配置 `TG_TARGET_CHAT` 时会用到）。

## 使用

```
/sync https://x.com/NASA/status/2119083432835764635
```

一次多个：

```
/sync https://x.com/a/status/123 https://t.me/somechannel/456
```

内置命令：

| 命令 | 说明 |
| --- | --- |
| `/sync [--download] <url> …` | 下载/转发并回传（回复消息带暂停/取消按钮） |
| `/tasks` | 查看**进行中**的任务（排队/运行/暂停），可逐条暂停、取消、继续 |
| `/retry <任务ID>` | 重新执行失败/取消的任务（失败消息上也有「🔄 重试」按钮） |

`--download`（或 `-d`）只对 t.me 链接生效：强制走「下载 → 上传」而不是服务端转发，
用来验证回落逻辑或确实需要一份独立副本时使用。
| `/id` | 查看 chat_id / user_id |
| `/help` | 帮助 |

任务状态：`queued` → `running` → `done` / `failed` / `cancelled`，以及可恢复的 `paused`。
`/tasks` 只列出 `queued` / `running` / `paused` 三种。

失败和取消是正常终态，不会自动重试；状态消息上会保留 `🔄 重试` 按钮，
也可以随时用 `/retry <任务ID>` 重新排队（任务 ID 就是状态消息里的 `🆔 a1b2c3d4`）。
重试会复用同一条任务记录和同一条状态消息。

## 部署为服务（systemd）

```bash
sudo cp deploy/tg-sync.service /etc/systemd/system/
sudo editor /etc/systemd/system/tg-sync.service   # 改成你的路径和用户
sudo systemctl daemon-reload
sudo systemctl enable --now tg-sync
journalctl -u tg-sync -f
```

## 调试

只下载、不上传（X 链接不需要任何凭证）：

```bash
.venv/bin/tg-sync download "https://x.com/NASA/status/2119083432835764635" -o /tmp/out
```

## 工作流程细节

1. 机器人收到 `/sync` 命令（`/sync@botname` 也接受），先校验发送者在 `TG_ALLOWED_USERS` 中。
2. 命令写入 SQLite 任务表（`state/tasks.db`），同一 `(chat_id, message_id)` 唯一，重复投递不会重复处理。
   状态消息立刻以「回复命令」的形式发出，并带 `⏸ 暂停` / `✖ 取消` 按钮。
3. 启动时：`running` 的任务改回 `queued`、`queued` 的重新入队、`paused` 保持暂停——
   所以重启、崩溃、网络中断都不会丢任务。
4. 按链接类型选择下载器：
   - **X**：先读 X 的公开 syndication 接口拿到媒体清单 → 图片按 `name=orig` 原图下载；
     视频按码率从高到低挑选能放进上传上限的 mp4 直链（实测比 yt-dlp 的 HLS 快约 10 倍），
     直链失败才回落到 yt-dlp（此时可用 cookies）。
   - **t.me**：优先**服务端转发**（`messages.forwardMessages` + `drop_author`，等价复制，
     不下载、不占带宽、相册整组搬）。频道禁止转发（`noforwards`）时自动回落到
     「`download_media` 落盘 → 重新上传」。相册用 `channels.getMessages` 按 id 列表展开
     （机器人不能调用 `messages.getHistory`）。
5. 上传到 `TG_TARGET_CHAT`（未设置则命令所在聊天），`reply_to` 指向命令消息；
   首条带标题和原始链接作为说明。视频会用 `ffprobe` 读时长/分辨率、`ffmpeg` 抽一帧封面，
   通过 `DocumentAttributeVideo` + 缩略图上传（否则客户端不显示时长和封面；Telethon 内置的
   元数据读取依赖 mutagen，未安装时会退化成 duration=0）。相册里的视频同样带元数据
   （手动走 `uploadMedia` + `sendMultiMedia`，因为 Telethon 的相册路径不支持 attributes/thumb）。
6. 全部成功则删除进度消息；有失败则把它改成汇总（成功数 + 失败原因）。
7. 暂停/取消会立即中断正在跑的下载或上传（上传是原子的，中断不会留下半条消息）；
   「继续」会从头重新执行该任务。
8. 下载直链有停滞看门狗：30s 无数据或平均速率低于 64 KiB/s 就判定连接被限速，
   自动换新连接重试（最多 3 次），避免一条坏 TCP 连接把任务拖死。

## 目录结构

```
src/tg_sync/
├── app.py                  # 命令入口、授权、worker 池、按钮回调、进度消息
├── cli.py                  # run / login / download
├── commands.py             # /sync 解析、命令名提取
├── config.py               # 环境变量 / .env
├── links.py                # X / t.me 链接归一化
├── models.py               # MediaItem
├── tasks.py                # SQLite 任务队列（持久化、状态机）
├── uploader.py             # MTProto 上传、相册分块、体积限制
└── downloaders/
    ├── x.py                # syndication + yt-dlp 双通道
    └── telegram.py         # Telethon 直读
```

## 开发

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
```

## 已知限制

- 机器人读不到任何人的收藏夹；结果只能发到与机器人的对话或 `TG_TARGET_CHAT`。
- t.me 链接要求机器人能访问该消息：公开频道通常可以，私有频道需要把机器人加为成员/管理员
  （机器人无法自己加入频道）。
- t.me 优先转发；受保护频道（禁止转发/保存）会回落到下载后上传，若连保存也被禁止则任务失败，
  失败消息上可以点「🔄 重试」。
- X 的受保护推文、仅登录可见内容需要配置 cookies；`syndication` 是公开接口，
  X 调整后可能失效，届时会自动回落到 yt-dlp。
- 上传上限 2000 MiB（MTProto 机器人上限），受 `TG_MAX_FILE_SIZE_MB` 控制。
- 只处理新消息，不会回溯历史；需要补历史时重新发命令即可（去重按消息 ID）。
- 暂停/继续是「从头重跑」，不做断点续传（Telegram 上传本身是原子的）。
- 任务表不会自动清理，长期使用可定期删除终态记录（`state/tasks.db`）。
- bot token、`.env`、`state/`、`downloads/` 已在 `.gitignore` 中排除，请勿外泄。
