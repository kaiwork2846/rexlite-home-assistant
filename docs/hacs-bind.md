# HACS 直接綁定（`rexlite/hacs/bind`）

HACS 自己的設定流程只有 GitHub 裝置登入，現場需要 GitHub 帳號；經銷商多半沒有。MAX'Is運營平台改用公司的 GitHub 機器帳號權杖，透過本整合直接綁定 HACS。

## 指令

`rexlite/hacs/bind`（admin WebSocket）

| 欄位 | 說明 |
| --- | --- |
| `token` | GitHub 權杖（classic `ghp_…`／`gho_…` 或 fine-grained `github_pat_…`） |

成功回 `{"bound": true, "created": <是否新建>, "state": "<HACS 設定項目狀態>"}`。

1. 權杖格式不對 → `hacs_token_invalid`；主機沒有 HACS 檔案 → `hacs_not_installed`。
2. 以權杖呼叫 GitHub `GET /rate_limit`：401 → `hacs_token_invalid`；403／429 或剩餘 0 → `hacs_rate_limited`；其他狀態 → `hacs_github_unavailable`；連不上 → `hacs_github_unreachable`。
3. 沒有 HACS 設定項目：建立與 HACS 裝置登入相同的設定項目（`data={"token"}`、`options={"experimental": true}`、version 1）並載入。
4. 已有設定項目：停用中 → `hacs_disabled`；結束等待中的 reauth 流程；權杖有變或未載入時，經 HA `async_unload`（同時移除 HACS 的 update listener）→ 更新權杖 → `async_setup`。卸載失敗 → `hacs_busy`；狀態無法恢復 → `hacs_restart_required`。

## 權杖

- 存在 HA MCP 的 `HACS_GITHUB_TOKENS`（可多個，逗號分隔；每台主機固定分到同一個，分攤 GitHub 每小時 5000 次的上限）。
- 用專用的 GitHub 機器帳號；classic 權杖不勾任何 scope，或 fine-grained 權杖只給公開唯讀。HACS 只讀公開倉庫。
- 權杖不寫日誌、不回傳；只存在 HACS 的設定項目（HACS 需要它）。指令結構只收 `str`，避免 HA 的 invalid_format 訊息把權杖帶回。
