# rexlite-home-assistant（給 Claude 的工作說明）

## 發版步驟（每一版都照做）

1. `custom_components/rexlite/manifest.json` 的 `version` 與 `const.py` 的 `INTEGRATION_VERSION` 改成新版號。
2. `CHANGELOG.md`：把 `## Unreleased` 改成 `## X.Y.Z`（最上面一節必須是新版號）。
3. `README.md` 最上面的「**目前版本：`X.Y.Z`**」改成新版號。完整歷史只寫在 CHANGELOG，README 不再逐版補說明。
4. `PYTHON_BIN=<HA venv python> bash scripts/verify.sh` 通過——`tests/test_release_version.py` 會擋上面三處版號不一致。
5. 合併後打 tag `vX.Y.Z` 並建 GitHub Release；**HACS 只看 Release，沒有 Release 商店不會更新**。

## 慣例

- 從 fork（kaiwork2846）開 PR 到 upstream `rexliteai/rexlite-home-assistant` 的 `main`。
- 改 KNX 部署／mapper 時，雲端（maxisos 的 ha-mcp-server、web-operation-backend）會驗證回傳格式：例如 `completed` 不能有 `skipped`，改回傳格式前先查 `core/ha-core/knx_project_deployment.go`。
