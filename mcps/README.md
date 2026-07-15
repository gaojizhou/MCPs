# MCP 收录约定

`mcps/` 中的每个一级目录代表一个可以独立安装、配置和启动的 MCP 服务。

建议结构：

```text
mcps/example-mcp/
├── README.md            # 必需：安装、配置、工具、安全边界与排错
├── pyproject.toml        # Python 项目按需使用
├── package.json          # Node.js 项目按需使用
├── requirements.txt     # 简单 Python 服务按需使用
├── src/                 # 代码较多时使用
├── tests/               # 自动化测试
└── .env.example         # 只写变量名和安全的示例值，绝不写真实凭据
```

## 必要约定

1. 目录使用小写连字符命名，例如 `filesystem-reader`。
2. 依赖属于具体 MCP，不放到仓库根目录。
3. README 必须给出可直接修改的 MCP 客户端配置示例。
4. 凭据通过环境变量或密钥存储传入，不能硬编码或提交。
5. 生成文件、模型权重和运行输出应加入根目录 `.gitignore`。
6. 新增 MCP 后更新仓库根 README 的 MCP 列表。
7. 不假设所有 MCP 使用同一种语言或共享同一个虚拟环境。

如果多个 MCP 后来确实共享稳定代码，可以再引入 `shared/`；在出现真实复用之前，优先
保持各个 MCP 自包含。
