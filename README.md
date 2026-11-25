# pocket-mcp

**给手机用的 MCP 服务器**：一个单文件、纯 Python 标准库、无需 `pip install`、无状态的 MCP 服务器。在电脑或云主机上开一个 HTTP 端口，任何支持 streamable HTTP 的 MCP 客户端（手机聊天 App、桌面客户端、脚本）就能通过 `http://<主机IP>:<端口>/mcp` 使用时间、计算、文件、搜索、命令和网页抓取工具。

它使用 JSON-RPC 2.0 over HTTP（streamable HTTP 的 JSON 响应模式）：只处理 `POST /mcp`，不提供 SSE，也不需要会话。每个请求独立处理，重启服务器不会丢状态。

典型场景：手机上的 AI 聊天客户端本身跑不了 shell / 读不了电脑上的文件，把 pocket-mcp 跑在电脑或云主机上，手机连过来就能"借用"这些能力。作者的另一款 Android 客户端 [DeepSeek Lite](https://github.com/YYUN64/DeepSeekChat) 就是拿它当工具后端的。

> 部署只要一条命令：`python3 server.py --host 0.0.0.0 --port 8765`，详见下文。

---

## 三种用起来的方式

| 方式 | 一条命令 | 需要 root？ |
| --- | --- | --- |
| **直接跑**（最简单） | `curl -fsSL https://raw.githubusercontent.com/YYUN64/pocket-mcp/main/run.sh \| bash` | ❌ 不需要 |
| **用户级常驻**（登录即起） | `./install.sh --user` | ❌ 不需要 |
| **系统级服务**（机器级部署） | `sudo ./install.sh` | ✅ 需要 |

### 1. 直接跑（无 sudo、无安装）

```bash
# 已 clone 到本地
./run.sh                      # 前台运行，0.0.0.0:8765
./run.sh --port 9000 --daemon # 后台运行，日志在 ~/.local/share/pocket-mcp/server.log
./run.sh --stop               # 停掉后台实例

# 没有 clone：一条命令下载并运行
curl -fsSL https://raw.githubusercontent.com/YYUN64/pocket-mcp/main/run.sh | bash
```

`run.sh` **不写 `/etc`、不建用户、不需要 sudo**，全部以当前用户身份运行；`server.py` 会下载到 `~/.local/share/pocket-mcp/`，工作区默认 `~/pocket-mcp-workspace`。

### 2. 用户级常驻（仍然不需要 sudo）

```bash
./install.sh --user                          # 装到 ~/.local/share/pocket-mcp
./install.sh --user --port 9000 --allow-shell
./install.sh --user --uninstall
```

写的是 `~/.config/systemd/user/pocket-mcp.service`，用 `systemctl --user` 管理。想让它在**不登录时也开机自启**，再执行一次：

```bash
loginctl enable-linger $USER
```

### 3. 系统级服务（需要 sudo）

```bash
sudo ./install.sh
```

只有在「整台机器共用一个服务、要用专用系统用户」时才需要 root。它会创建 `pocketmcp` 用户并把服务装到 `/etc/systemd/system`。

### 三种方式的区别

| | run.sh | install.sh --user | sudo install.sh |
| --- | --- | --- | --- |
| root | 不需要 | 不需要 | 需要 |
| 开机自启 | ❌（除非自己加 cron/计划任务） | ✅（配合 linger） | ✅ |
| 专用用户 | 用你自己 | 用你自己 | `pocketmcp` |
| 沙箱 | 无（就你本人的权限） | systemd 用户级沙箱 | systemd 系统级沙箱 + 空 capability |

> 说明：用户级模式下 `systemd-analyze --user security` 会比系统级差（实测 8.1 EXPOSED vs 4.4 OK）。原因是普通用户的 systemd 没有 `CAP_SYS_ADMIN`/`CAP_SETPCAP`，`PrivateDevices`、`ProtectKernel*`、空的 `CapabilityBoundingSet=` 这类选项会让服务**直接起不来**（`status=218/CAPABILITIES`），所以用户级只保留能用的那部分。用户级的安全边界来自"它就是你自己的权限"，要更强隔离就用系统级。

## 鉴权（token）

服务器默认**不做鉴权**（历史上一直如此），谁连上端口谁就能调用工具。公网部署请务必设置 `MCP_TOKEN`：设置后所有 `/mcp` 请求必须带 `Authorization: Bearer <token>`，不匹配返回 `401` + JSON-RPC error `-32001`。

```bash
# 1) 生成一个随机 token（64 位十六进制）
TOKEN="$(openssl rand -hex 32)"
echo "$TOKEN"

# 2) 直接跑
MCP_TOKEN="$TOKEN" ./run.sh --daemon
# 或者
./run.sh --token "$TOKEN" --daemon

# 3) 装成服务（写进 systemd 的 Environment=MCP_TOKEN=...）
./install.sh --user --token "$TOKEN"
sudo ./install.sh --token "$TOKEN"
```

App 侧：设置 → MCP 服务器 → **Token** 填同一个值（留空表示不鉴权）。App 的「测试连接」也会带 token。

自测（把 `<token>` 换成你的值）：

```bash
# 没有 token：应返回 401 + -32001
curl -s -i -X POST http://127.0.0.1:8765/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'

# 带正确 token：应返回 200 + result
curl -s -i -X POST http://127.0.0.1:8765/mcp \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <token>' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'

# 冒烟测试（有 token 时会额外测“无 token/错误 token -> 401”，并跳过相关用例）
python smoke_test.py http://127.0.0.1:8765 --token '<token>'
```

说明：

- token 会写进 systemd 单元（`Environment=MCP_TOKEN=...`），单元文件对 root 可见；不要把带 token 的单元文件提交到仓库。
- 轮换 token：重新执行一次 `./install.sh --token '<新 token>'`（或改 `MCP_TOKEN` 后重启服务），App 里也改成新值。
- 只监听本机时（`--bind 127.0.0.1` + SSH 隧道）可以不设 token，但一旦对公网开放就必须设。

## 安全模型（为什么不该用 root）

| 风险点 | 说明 |
| --- | --- |
| `run_command` | **默认禁用**。一旦 `--allow-shell` 打开，等于给这个用户一个远程 shell；若服务以 root 跑，那就是**公网 root shell** |
| 文件工具 | 只能读写 `MCP_WORKSPACE` 内的路径（resolve 后校验，`../` 越界被拒）；如果把工作区设成 `/`，等于全盘可读写 |
| 无鉴权 | 服务器默认没有账号密码，**谁连上谁就能用**；公网部署请设置 `MCP_TOKEN`（见上一节），否则不要裸奔在公网 |

部署方式按安全性从高到低：

1. `--bind 127.0.0.1` + SSH 隧道或 Tailscale（最稳，连局域网都不暴露）；
2. 监听局域网 + 防火墙只放行你的网段（脚本会打印对应命令）；
3. 直接暴露公网：至少套一层 WireGuard/Tailscale，或放在带认证的反向代理后面。

脚本默认提供的防护是：**非 root 专用用户 + systemd 沙箱 + 工作区隔离**。即使服务被攻破，攻击者拿到的也只是 `pocketmcp` 用户和 `/srv/pocket-mcp-workspace` 一个目录。

---

## 1. 环境要求

- Python **3.8+**（Windows 推荐 Python 3.11/3.12/3.13/3.14 的 64 位版本；Ubuntu/Debian/Arch/Fedora 的系统 Python 即可）。
- 不需要安装 fastapi、flask、uvicorn 等任何第三方包。
- 不需要数据库、不需要 Node.js。
- 默认监听 `0.0.0.0:8765`，MCP 端点为 `POST /mcp`。

目录内文件：

| 文件 | 用途 |
| --- | --- |
| `server.py` | 服务器主程序，单文件，包含全部 8 个工具。 |
| `smoke_test.py` | 自检脚本：initialize、tools/list、每个工具、错误码、越界路径。 |
| `install.sh` | Linux 安装：`--user` 用户级（无 sudo）/ 默认系统级（sudo）+ systemd 沙箱 + 自检 + 防火墙提示；`--uninstall` 卸载。 |
| `run.sh` | 直接运行脚本：一条 `curl … | bash` 即可，无需 root、无需安装；支持 `--daemon` / `--stop`。 |
| `mcp-server.service` | systemd 单元模板（含开机自启），需要替换占位符。 |
| `run-windows.bat` | Windows 一键启动；优先 `py -3`，找不到再退回 `python`。 |
| `README.md` | 本文档。 |

### 平台与兼容性

- 同一份 `server.py` 同时支持 Linux 和 Windows，使用 `pathlib`、`subprocess`、`urllib`，没有使用 `fcntl`、`os.fork`、`signal` 等 POSIX-only 接口。
- 已在 **Windows 10 + Python 3.14.10** 和 **Ubuntu 26.10 + Python 3.14.8** 上实际启动并通过自检（Linux 上还实际启动过 systemd 单元）。
- 目标是 Python **3.8+**；代码已用 Python 3.8 语法模式做静态解析，并人工核对未使用 3.9+ 才有的运行期 API。没有在 3.8 解释器上实跑。Arch/Fedora 使用系统 Python 和标准包管理器命令，未单独实机测试。

---

## 2. 先准备代码目录和工作区

文件工具只能访问“工作区”内的路径，越界会被拒绝。建议把代码和工作区分开。

### Linux

```bash
sudo mkdir -p /opt/deepseek-lite-mcp
sudo mkdir -p /srv/mcp-workspace
# 把 server.py、smoke_test.py、mcp-server.service 放到 /opt/deepseek-lite-mcp
sudo chown -R "$USER":"$USER" /srv/mcp-workspace
```

### Windows

```bat
mkdir D:\mcp
mkdir D:\mcp-workspace
REM 把 server.py、smoke_test.py、run-windows.bat 放到 D:\mcp
```

实际路径可以不同，下文出现 `D:\mcp`、`/opt/deepseek-lite-mcp` 时替换成你的路径。

工作区优先级：

1. 环境变量 `MCP_WORKSPACE`
2. 启动参数 `--workspace`
3. 当前工作目录（服务器启动时所在目录）

---

## 3. 启动服务器

### Ubuntu / Debian

```bash
sudo apt update
sudo apt install -y python3
cd /opt/deepseek-lite-mcp
export MCP_WORKSPACE=/srv/mcp-workspace
python3 server.py --host 0.0.0.0 --port 8765
```

### Arch Linux

```bash
sudo pacman -S --needed python
cd /opt/deepseek-lite-mcp
export MCP_WORKSPACE=/srv/mcp-workspace
python3 server.py --host 0.0.0.0 --port 8765
```

### Fedora

```bash
sudo dnf install -y python3
cd /opt/deepseek-lite-mcp
export MCP_WORKSPACE=/srv/mcp-workspace
python3 server.py --host 0.0.0.0 --port 8765
```

### Windows

双击 `run-windows.bat`，或在命令提示符中：

```bat
cd /d D:\mcp
set MCP_WORKSPACE=D:\mcp-workspace
run-windows.bat
```

也可以直接：

```bat
py -3 server.py --host 0.0.0.0 --port 8765 --workspace D:\mcp-workspace
```

PowerShell：

```powershell
cd D:\mcp
$env:MCP_WORKSPACE = "D:\mcp-workspace"
py -3 .\server.py --host 0.0.0.0 --port 8765
```

### 端口和参数

```bash
python3 server.py 9000                  # 位置参数也可以指定端口
python3 server.py --port 9000          # 推荐写法
python3 server.py --host 0.0.0.0 --workspace /srv/mcp-workspace
```

Windows 同理：

```bat
run-windows.bat --port 9000 --workspace D:\mcp-workspace
```

默认端口是 `8765`；`--host` 默认是 `0.0.0.0`。如果端口被占用，程序会直接退出并打印“端口已被占用”，换一个端口或先结束旧进程即可。Linux 上可查：

```bash
sudo ss -ltnp | grep 8765
```

Windows 上可查：

```powershell
Get-NetTCPConnection -LocalPort 8765 -State Listen
```

日志全部输出到 **stderr**，每个 HTTP 请求一行，包含方法名和耗时，例如：

```text
[2026-11-17 20:23:48] POST /mcp -> 200 method=tools/call 0.3ms
```

---

## 4. 放行防火墙

### ufw（Ubuntu / Debian）

```bash
sudo ufw allow 8765/tcp
sudo ufw status
```

只允许同一局域网访问更安全（把网段换成你的）：

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8765 proto tcp
```

### firewalld（Fedora 等）

```bash
sudo firewall-cmd --permanent --add-port=8765/tcp
sudo firewall-cmd --reload
sudo firewall-cmd --list-ports
```

只允许局域网网段：

```bash
sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="192.168.1.0/24" port protocol="tcp" port="8765" accept'
sudo firewall-cmd --reload
```

### Windows 防火墙

以管理员身份打开 PowerShell：

```powershell
New-NetFirewallRule -DisplayName "deepseek-lite-mcp 8765" `
  -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8765 -Profile Private
```

删除规则（如需要）：

```powershell
Remove-NetFirewallRule -DisplayName "deepseek-lite-mcp 8765"
```

建议只对“专用网络/Private”放行，不要把端口直接暴露到公网。

---

## 5. systemd 服务与开机自启（Linux）

仓库里的 `mcp-server.service` 是模板，需要把占位符替换成真实值：

| 占位符 | 示例 |
| --- | --- |
| `@WORKDIR@` | `/opt/deepseek-lite-mcp` |
| `@PYTHON@` | `/usr/bin/python3` |
| `@USER@` | `mcpuser` |
| `@GROUP@` | `mcpuser` |
| `@WORKSPACE@` | `/srv/mcp-workspace`（可选，默认注释） |

建议创建专用低权限用户：

```bash
sudo useradd --system --home /opt/deepseek-lite-mcp --shell /usr/sbin/nologin mcpuser
sudo chown -R mcpuser:mcpuser /opt/deepseek-lite-mcp /srv/mcp-workspace
```

安装并编辑服务：

```bash
sudo cp mcp-server.service /etc/systemd/system/mcp-server.service
sudo nano /etc/systemd/system/mcp-server.service
```

将 `mcp-server.service` 中的 `WorkingDirectory`、`ExecStart`、`User`、`Group` 改好。若工作区不是 `WorkingDirectory`，取消 `Environment=MCP_WORKSPACE=...` 的注释并改成绝对路径。

启用并开机自启：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mcp-server
systemctl status mcp-server --no-pager
```

查看日志：

```bash
journalctl -u mcp-server -f
```

修改配置后重启：

```bash
sudo systemctl restart mcp-server
```

停止或取消开机自启：

```bash
sudo systemctl disable --now mcp-server
```

> 注意：`User=` 和 `Group=` 不能保留占位符，否则 systemd 无法启动；工作区目录必须对该用户可读写。默认不要开启 `MCP_ALLOW_SHELL`。

---

## 6. Windows 后台运行：bat + 计划任务

`run-windows.bat` 本身是前台进程，关闭窗口就停止。要开机后台运行，用 Windows 任务计划程序托管它。

### 图形界面方式

1. `Win + R` 输入 `taskschd.msc`，打开“任务计划程序”。
2. 右侧“创建任务”（不是“创建基本任务”）。
3. **常规**：名称填 `deepseek-lite-mcp`；选择“不管用户是否登录都要运行”（需要保存账户密码），或“只在用户登录时运行”。
4. **触发器** → 新建 → “启动时”（或“登录时”）。
5. **操作** → 新建 → “启动程序”：
   - 程序或脚本：`D:\mcp\run-windows.bat`
   - 起始于：`D:\mcp`
6. **条件**：取消“仅当计算机使用交流电源时才启动”。
7. **设置**：勾选“如果任务失败，按以下频率重新启动”，例如 1 分钟、3 次。
8. 保存后右键任务 → “运行”测试。

### 命令行方式（管理员 cmd）

```bat
schtasks /Create /TN "deepseek-lite-mcp" /SC ONSTART ^
  /TR "D:\mcp\run-windows.bat" /RU "%USERNAME%" /RP * /F
```

执行后会提示输入该 Windows 账户密码。路径含空格时需要用引号正确转义。立即运行：

```bat
schtasks /Run /TN "deepseek-lite-mcp"
schtasks /Query /TN "deepseek-lite-mcp" /V /FO LIST
```

停止/删除：

```bat
schtasks /End /TN "deepseek-lite-mcp"
schtasks /Delete /TN "deepseek-lite-mcp" /F
```

如需保留后台日志，可把“操作”改成：

- 程序或脚本：`cmd.exe`
- 添加参数：`/c ""D:\mcp\run-windows.bat" >> "D:\mcp\server.log" 2>&1"`

---

## 7. 用 curl 自测

先确认服务器已启动。以下命令在电脑本机执行；把 `127.0.0.1` 换成局域网 IP 也可以。

### 初始化

```bash
curl -sS -X POST http://127.0.0.1:8765/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}'
```

Linux/macOS 使用上面的单引号写法。Windows PowerShell 建议把 JSON 通过管道交给 curl，避免 PowerShell 与 `curl.exe` 之间反复转义引号：

```powershell
$body = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}'
$body | curl.exe -sS -X POST http://127.0.0.1:8765/mcp -H "Content-Type: application/json" --data-binary "@-"
```

应返回 `result.serverInfo.name = deepseek-lite-mcp`，HTTP 状态 200。

### 列出工具

```bash
curl -sS -X POST http://127.0.0.1:8765/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```

### 调用工具

```bash
curl -sS -X POST http://127.0.0.1:8765/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_time","arguments":{}}}'
```

计算：

```bash
curl -sS -X POST http://127.0.0.1:8765/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"calc","arguments":{"expression":"(3+4)*2"}}}'
```

返回内容里的 `content[0].text` 应为 `14`。

### 自检脚本

```bash
python3 smoke_test.py http://127.0.0.1:8765
```

如果服务器的 `MCP_ALLOW_SHELL=1`：

```bash
python3 smoke_test.py http://127.0.0.1:8765 --allow-shell
```

默认情况下 `run_command` 被禁用，自检脚本会把它报告为符合预期的禁用行为；`http_fetch` 如果因为当前机器没有外网而失败，会显示 `SKIP` 并以 0 退出。想强制网络成功可用：

```bash
python3 smoke_test.py http://127.0.0.1:8765 --strict-http
```

---

## 8. Android App 里怎么填

1. 确保手机和电脑连接同一个 Wi-Fi/局域网。
2. 查电脑的局域网 IP：
   - Windows：`ipconfig`，看“IPv4 地址”，例如 `192.168.1.23`。
   - Ubuntu/Debian：`hostname -I` 或 `ip addr`，例如 `192.168.1.23`。
3. 在 Android 聊天客户端的 MCP 服务器设置中填写：

```text
名称：任意，例如 DeepSeek Lite
地址/URL：http://192.168.1.23:8765/mcp
传输方式：Streamable HTTP / HTTP（JSON）
认证：无 / 留空（服务器设了 MCP_TOKEN 时填同一个 token）
```

如果 App 要求分别填写 IP 和端口：

```text
IP：192.168.1.23
端口：8765
路径：/mcp
```

注意事项：

- 不要填 `127.0.0.1` 或 `localhost`，那会指向手机自己。
- 端口必须和启动参数一致（默认 8765）。
- 路径必须是 `/mcp`，不是 `/`。
- 如果 App 只支持 SSE 传输，就不能用本服务器；本服务器只返回 `application/json`，不提供 SSE。
- 可先用手机浏览器打开 `http://192.168.1.23:8765/`，能看到说明页就说明网络和防火墙已通。

---

## 9. 工具列表

所有工具名都是 `snake_case`。工具执行失败时返回 `result.isError = true`，错误原因写在 `content[0].text`，不会用 JSON-RPC error 表示工具失败。

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `get_time` | 无 | 服务器本地日期、时间、星期、时区。 |
| `calc` | `expression` | 四则运算；仅数字、`+ - * / % ** //`、括号、一元负号，使用 `ast`，不使用 `eval`。 |
| `list_dir` | `path`（默认 `.`） | 列出工作区内目录的名称、类型、大小。 |
| `read_file` | `path`、`max_bytes`（默认 200000） | 读取 UTF-8 文本；二进制文件返回提示。 |
| `write_file` | `path`、`content` | 写入 UTF-8 文本，自动创建父目录，返回字节数。 |
| `search_files` | `pattern`、`file_glob`（默认 `**/*`）、`max_results`（默认 100） | 正则搜索，返回 `路径:行号: 行内容`。 |
| `run_command` | `command`、`timeout`（默认 30 秒） | 默认禁用；仅 `MCP_ALLOW_SHELL=1` 时可用。 |
| `http_fetch` | `url`、`max_bytes`（默认 200000）、`strip_html`（默认 true） | urllib 抓取，带 User-Agent、跟随重定向、20 秒超时。 |

统一限制：

- 请求体最大 2 MiB，超过返回 HTTP 400。
- 单个工具输出最多约 200000 字符 / 4000 行。
- 文件路径必须 `resolve()` 后仍位于工作区，`../../etc/passwd` 之类的路径会被拒绝。
- 搜索会跳过二进制文件、单文件超过 2 MB 的文件，并限制总扫描量。

---

## 10. 安全注意事项（务必阅读）

1. **默认没有身份认证（可以用 `MCP_TOKEN` 开启 Bearer token），也没有 TLS。** 不设 token 时任何能访问 `IP:8765` 的人都可以读取/写入工作区。公网/不可信网络请设置 token，并尽量再加 TLS。
2. **不要裸奔公网端口映射。** 确实需要公网访问时，请设置 `MCP_TOKEN`，并在前面加 HTTPS 反向代理 + 来源 IP 限制。
3. **`run_command` 默认关闭。** 设置 `MCP_ALLOW_SHELL=1` 后，任何能访问端口的人都等同于拥有一个 shell；没有命令白名单。仅在临时调试时开启，用完关闭并重启。
4. **使用低权限专用用户运行**，不要把工作区设成 `/`、`/etc`、用户主目录或包含密钥/凭据的目录。`MCP_WORKSPACE` 应该是一个专用空目录。
5. `list_dir` / `read_file` / `write_file` / `search_files` 会校验路径是否在工作区内，并尽量拒绝指向工作区外的符号链接；但它不是操作系统级沙箱，写入前仍有极小的 TOCTOU 竞态窗口，不能把它当作对抗恶意客户端的隔离机制。
6. `http_fetch` 可以访问服务器所在网络内的地址（存在 SSRF 风险），不要把它暴露给不可信客户端。
7. 日志会记录请求方法、路径、状态和耗时，不记录请求正文；生产环境注意日志轮转和访问控制。
8. 如果启用 shell，命令输出和文件内容可能包含敏感信息，并通过 MCP 返回给客户端。

---

## 11. 常见问题

**端口被占用**

换端口：

```bash
python3 server.py --port 8766
```

Windows：

```bat
run-windows.bat --port 8766
```

或结束旧进程：

```bash
# Linux
sudo ss -ltnp | grep 8765
sudo kill <PID>
```

```powershell
# Windows
Get-NetTCPConnection -LocalPort 8765 -State Listen | Select-Object OwningProcess
Stop-Process -Id <PID>
```

**手机连不上**

- 服务器是否用 `0.0.0.0` 监听，而不是只监听 `127.0.0.1`。
- 防火墙是否放行 8765/TCP，Windows 当前网络是否为“专用网络”。
- 手机和电脑是否在同一网段（有些路由器开启 AP 隔离会阻断互访）。
- 手机浏览器能否打开 `http://<电脑IP>:8765/`。
- 公司/学校 Wi-Fi 可能禁止设备互访，改用手机热点测试。

**`run_command` 一直返回禁用**

环境变量必须在服务器进程启动前设置，且值恰好为 `1`。设置后重启服务器：

```bash
export MCP_ALLOW_SHELL=1
python3 server.py --host 0.0.0.0 --port 8765
```

```bat
set MCP_ALLOW_SHELL=1
run-windows.bat
```

**`http_fetch` 报 SSL/网络错误**

服务器到目标站点的网络不通、被代理/防火墙拦截，或证书问题。先用 `curl -I https://example.com` 在同一台服务器上确认网络。错误会作为 `isError` 文本返回，不会让进程崩溃。

**Windows 日志/中文显示乱码**

`run-windows.bat` 会执行 `chcp 65001`。如果通过计划任务或重定向捕获日志仍乱码，可在启动前设置：

```bat
set PYTHONIOENCODING=utf-8
```

---

## 12. 实现与限制说明

- 传输：仅 `POST /mcp`，JSON-RPC 2.0，无 SSE、无会话、无批量请求。
- 无状态：每个 POST 单独处理，initialize 也不创建服务器端会话。
- 响应：成功和工具错误都返回 HTTP 200 + `application/json; charset=utf-8`；通知 `notifications/initialized` 返回 202 空体；解析失败返回 400 + `-32700`；未知方法返回 200 + `-32601`。
- 纯标准库：`http.server.ThreadingHTTPServer`、`urllib`、`pathlib`、`subprocess`、`ast` 等。
- 不支持文件删除、重命名、上传二进制；`read_file` 只按 UTF-8 文本读取（搜索会额外尝试系统本地编码）。
- 适合个人/局域网小工具，不适合多用户、公网或无信任环境。

---

## License

MIT，见 [LICENSE](LICENSE)。

