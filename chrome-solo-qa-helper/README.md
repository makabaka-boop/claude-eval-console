# Claude Eval → SOLO-QA 提交助手

该扩展只连接两个固定地址：

- `http://127.0.0.1:8765`：读取本地已完成轮次和轨迹，回写提交状态。
- `https://solo2.jzxhnh.com`：使用当前 Chrome 登录状态读取表单结构、上传轨迹、提交数据并读取质检结果。

扩展不读取或保存用户名、密码、Cookie，也不接受页面传入任意网络地址。最终提交必须从本地导出页面点击“提交到 SOLO-QA”并确认后才会发生。

## 安装

1. 在 Chrome 打开 `chrome://extensions`。
2. 开启右上角“开发者模式”。
3. 点击“加载已解压的扩展程序”。
4. 选择本目录：`/Users/zhangxinyu/claude code/claude-eval-console/chrome-solo-qa-helper`。
5. 刷新 `http://127.0.0.1:8765/#exports`。

安装后，导出页会显示“提交助手已连接”。历史状态只在点击“同步我的提交”时手动核对；提交按钮仅对本次所选轮次按 SessionID 和 TurnID 检查重复，然后上传并创建记录。同一 SessionID 的多轮记录会按轮次从小到大逐条提交；前序轮次还没有远端记录且没有同时选中时，页面会在上传前提示先选择前序轮次。
