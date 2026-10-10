# BOSS-HELP · 本机求职工作台

面向 Windows 的 BOSS 直聘辅助工具：在本机配置个人资料与筛选条件，查看岗位完整描述，生成岗位摘要和沟通草稿，逐条审核后再决定是否发送。

仓库仅包含源码、启动脚本、空白配置模板和测试。**首次使用时没有预选城市、岗位方向、毕业届别或个人简历，均由使用者自行填写。**

## 功能

- 在中文网页工作台中管理个人资料、城市、岗位方向、薪资条件和关键词。
- 通过 Chrome 扩展连接已经由用户登录的 BOSS 页面，读取岗位信息。
- 按配置筛选校招/应届和实习岗位，保存完整 JD、筛选依据与历史记录。
- 使用用户自行配置的模型服务生成摘要与招呼草稿。
- 编辑、批准或拒绝草稿；发送前显示具体岗位与原文，核对聊天记录后记录结果。
- 支持暂停、继续采集和历史去重；遇到平台验证或访问异常时保留暂停依据。

当前网页工作台主要围绕校招/应届和实习流程。仓库保留底层 CLI 功能，但其他平台或可选命令不属于本 README 的安装验证范围。

## 环境要求

- Windows 10/11 64 位、Windows PowerShell 5.1 和 Windows Script Host。
- Python **3.12**，并安装 Python Launcher（`py`）。已有其他 Python 路径时可使用下方的 PowerShell 命令。
- Chrome 浏览器，以及使用者自己的 BOSS 直聘账号。
- 首次安装需要联网下载 Python 依赖。模型服务的密钥由使用者自行提供。

## 安装与启动

### 1. 获取源码

```powershell
git clone https://github.com/xuxu69435-glitch/BOSS-HELP.git
cd BOSS-HELP
```

也可以下载 GitHub 的源码 ZIP 后解压。不要把源码放进另一个正在使用的安装目录。

### 2. 安装项目环境

双击 **`安装环境.bat`**。脚本在项目的 `venvs/boss-agent-cli/` 创建环境，并安装本地源码及网页工作台依赖。安装不会修改系统 PATH，也不会向系统 Python 安装包。

没有 `py` 命令时，在项目目录的 PowerShell 中指定自己的 Python 3.12：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_env.ps1 -PythonCommand 'C:\Python312\python.exe'
```

这里的路径只是示例，请替换为自己的 Python 路径。安装过程不会访问 BOSS、调用模型或发送消息。

### 3. 打开工作台并填写配置

双击 **`打开求职客户端.bat`**，在工作台完成以下配置：

1. **个人资料**：填写自己的资料标题、经历和必要的求职安排，然后保存。空白模板不代表任何真实个人。
2. **筛选条件**：自行选择城市、岗位方向或自定义方向关键词，填写毕业届别、月薪/日薪下限，以及批次数量，然后保存。
3. **模型服务**：使用自己的密钥。使用 DeepSeek 时可双击 `配置DeepSeek.bat`；输入内容隐藏，本机加密保存。该入口的默认接口与模型名称可在 `scripts/configure_deepseek.py` 查看；其他配置方式参见 CLI 帮助。

```powershell
.\一键启动.bat ai config --help
```

首次启动会将 `config/` 的空白模板复制到 `data/boss-agent/`，已有配置不会被覆盖。个人资料页面保存的新版本位于本机资料库中；手工修改 `resumes/user.json` 不会替代已保存的资料版本，日常编辑请使用工作台。

### 4. 连接自己的 Chrome

1. 在 Chrome 打开 `chrome://extensions`，开启开发者模式。
2. 点击“加载已解压的扩展程序”，选择本项目的 **`repos/boss-agent-cli/extension/`**。
3. 在 Chrome 中自行登录 BOSS，保留首页或职位页面。
4. 如桥接服务尚未启动，双击 `启动浏览器连接.bat`。
5. 回到工作台点击“连接 Chrome”，确认状态后再生成批次。

工具不会替使用者配置账号或自动解决验证码。遇到平台验证时，应先在 Chrome 中由本人处理。

### 5. 审核与发送

生成批次后，查看完整 JD、筛选依据、简历引用和待核实事项。可编辑招呼原文并逐条批准；编辑后需要重新审批。

发送前再次核对工作台显示的岗位和原文，并保留 BOSS 消息页面。需要使用者点击确认才会执行发送；结果不确定时应核查聊天记录，避免重复联系。不要把“连接成功”或“建立会话”当作正文发送成功。

## 数据与隐私

| 内容 | 本机保存位置 | 是否提交到 Git |
| --- | --- | --- |
| 空白配置模板 | `config/*.example.json` | 是 |
| 个人筛选条件 | `data/boss-agent/preferences/settings.json` | 否 |
| 简历与资料版本 | `data/boss-agent/resumes/`、`data/boss-agent/client-control/` | 否 |
| API 密钥与模型设置 | `data/boss-agent/ai/` | 否 |
| 登录会话、浏览器状态 | `data/boss-agent/auth/` 等运行目录 | 否 |
| 岗位、审批、聊天核验与运行日志 | `data/`、`logs/` | 否 |
| Python 环境、下载缓存与安装包 | `venvs/`、`tools/`、`dist/` 等 | 否 |

工作台 HTTP 服务仅绑定本机 `127.0.0.1`，使用启动令牌校验请求。打开工作台时的 URL 可能包含令牌，请勿截图分享或提交到仓库。

**“本机保存”不代表所有处理都离线。** 采集会访问 BOSS；生成文案会向你配置的模型服务发送岗位信息与用于生成的个人资料。资料处理会过滤标为手机、邮箱等联系字段，但正文和经历中仍可能包含个人信息，请自行检查并选择可信服务。API 密钥的本机加密不等于可以公开上传其文件。

`.gitignore` 已排除运行数据、凭证、日志、截图、环境和构建产物。提交前仍需检查 `git status --short` 和 `git diff --cached`；已被 Git 跟踪的文件不会因新增忽略规则而自动取消跟踪。不要使用 `git add -f` 强行加入个人数据。

## 目录结构

```text
BOSS-HELP/
├─ README.md                       中文安装与使用说明
├─ .gitignore                      本机数据与产物忽略规则
├─ config/                         不含个人信息的空白模板
├─ scripts/                        安装、初始化、启动与模型配置脚本
├─ repos/boss-agent-cli/
│  ├─ pyproject.toml                依赖与 CLI 入口
│  ├─ LICENSE                      上游 MIT 许可
│  ├─ src/boss_agent_cli/           Python 源码与网页资源
│  ├─ extension/                   Chrome 扩展源码
│  └─ tests/                       使用合成数据的回归测试
└─ *.bat / *.vbs                    Windows 双击入口
```

运行数据和环境由使用者本机生成。仓库不附带登录状态、私人安装包、浏览器可执行文件或开发过程截图。

## 开发与验证

依赖与工具都安装到项目环境：

```powershell
& '.\venvs\boss-agent-cli\Scripts\python.exe' -I -m pip --isolated install pytest
& '.\venvs\boss-agent-cli\Scripts\python.exe' -I -m pip --isolated check
& '.\venvs\boss-agent-cli\Scripts\python.exe' -I -m pytest '.\repos\boss-agent-cli\tests\test_first_run.py' '.\repos\boss-agent-cli\tests\test_client_profiles.py' '.\repos\boss-agent-cli\tests\test_search_plan.py' '.\repos\boss-agent-cli\tests\test_role_filters.py' -q
```

这些测试使用临时目录与合成资料。做真实联调前应自行检查测试或脚本的行为；不要在测试中复用真实简历、账号凭证或自动发送消息。

修改 Python 后退出并重启工作台；修改网页资源后刷新页面；修改扩展后在 Chrome 扩展管理页重新加载。虚拟环境不能直接跨电脑或路径复制，迁移时应重新安装并自行备份本机数据。

## 常见问题

- **提示找不到 Python 环境**：先运行 `安装环境.bat`，或通过 `-PythonCommand` 指定 Python 3.12。
- **工作台打不开**：检查 `logs/client-launcher.log` 和 `data/boss-agent/client-control/server.log`；确认 Windows Script Host 与 PowerShell 可用。
- **无法生成批次**：检查筛选条件是否已保存，城市、方向与届别是否填写完整；再检查个人资料、模型配置和 Chrome 连接。
- **Chrome 未连接**：确认扩展从正确目录加载，桥接服务已运行，且保留自己登录的 BOSS 页面。
- **模型调用失败**：检查自己选择的接口、模型、密钥和额度。配置密钥时不会联网验证其有效性。
- **部分 CLI 命令缺少依赖**：本安装只启用 `bridge` 和 `client`，其他功能的可选依赖见 `pyproject.toml`。
- **需要提供错误反馈**：先删除日志中的令牌、联系方式、简历、岗位记录等内容，再描述错误现象。

## 来源与许可

本项目基于 [boss-agent-cli](https://github.com/can4hou6joeng4/boss-agent-cli) 的 MIT 源码，加入本机中文工作台、资料编辑、审批与发送核验流程。上游作者和版权声明保留在 [LICENSE](repos/boss-agent-cli/LICENSE) 中；网页图标库的许可保留在其资源目录。

本项目与招聘平台无官方合作关系。请按平台规则使用，由本人确认筛选配置和每次发送内容。
