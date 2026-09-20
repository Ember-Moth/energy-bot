# 提交规范(Conventional Commits)

提交信息使用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/),**一律用英文书写**。

## 格式

```
<type>(<scope>): <subject>

[optional body]

[optional footer(s)]
```

## type 一览

| type | 用途 |
| --- | --- |
| `feat` | 新功能 |
| `fix` | 修复缺陷 |
| `docs` | 仅文档变更 |
| `style` | 不影响语义的格式调整(空格、分号等) |
| `refactor` | 既不是新增功能也不是修 bug 的代码变更 |
| `perf` | 提升性能的变更 |
| `test` | 新增或修改测试 |
| `build` | 影响构建系统或外部依赖的变更(uv、pyproject 等) |
| `ci` | CI 配置变更 |
| `chore` | 其他杂项(不修改 src 或测试) |
| `revert` | 回滚某次提交 |

## 规则

- subject 用祈使语气、英文小写开头、结尾不加句号,不超过 72 字符;
- scope 可选,常用模块名,如 `handlers`、`config`、`deps`;
- 破坏性变更在 footer 写 `BREAKING CHANGE: <描述>`,或 subject 后加 `!`(如 `feat(api)!:`);
- 关联 issue 写在 footer,如 `Closes: #123`。

## 示例

```
feat(handlers): add /status command for battery level
```

```
fix(config): reject non-https webhook base_url

Telegram requires HTTPS for webhook endpoints, so fail fast
at startup with a clear error instead of an API rejection.
```

```
refactor(main)!: extract webhook server setup into serve()

BREAKING CHANGE: main() no longer accepts a config path argument
```

## 工具支持

- **交互式提交**:`uv run cz commit`,按提示逐步生成规范的提交信息;
- **提交模板**:`git commit` 时会加载 `.gitmessage` 模板(仓库已配置 `commit.template`);
- **强制校验**:`.githooks/commit-msg` 会在每次提交时用 `cz check` 校验格式,不符合规范的提交会被拒绝(仓库已配置 `core.hooksPath` 指向 `.githooks`);
- **版本与变更日志**:`uv run cz bump` 依据提交记录自动升版本号、打 tag 并更新 `CHANGELOG.md`。
