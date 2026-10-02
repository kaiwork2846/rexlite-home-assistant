# Node-RED RS-485 橋接

運營平台「＋ → 冷氣／窗簾／電子鎖」精靈使用的主機端指令。全部為管理員 WebSocket 指令，經 HA MCP → Go core → tunnel 呼叫；本套件不開放任何新連接埠。

## Node-RED 連線

依序嘗試：

1. 平台設定的網址（`configure` mode=`url`）。Node-RED 未啟用 adminAuth 時直接連線；啟用時以 `/auth/token` 取得權杖；add-on 直連埠（nginx 驗證）使用 HTTP Basic。
2. Supervisor 上已安裝的 Node-RED add-on（優先 `a0d7b954_nodered`）。以 HA Core 的 Supervisor token 建立 ingress session 呼叫 Admin API，不需輸入帳密。add-on 停止時可用 `start_addon` 啟動。
3. 本機 `http://127.0.0.1:1880`、`http://172.17.0.1:1880`。

網址只接受區網 IP、loopback、單一名稱主機（Docker 服務名）與 `.local`；不接受帳密、query、公開 IP。帳密存於 `.storage/rexlite.node_red`（private），狀態只回傳 `hasPassword`。

## 指令

| type | 參數 | 回傳 |
| --- | --- | --- |
| `rexlite/nodered/status` | – | `api`、`templates`（此版本可部署的範本）、`nodeRed`（state: ready / stopped / auth_required / auth_failed / unreachable / not_installed / not_found）、`mqtt`（含 `suggestedBroker`）、`endpoint`、`bridges`（含心跳 `health`）、`job` |
| `rexlite/nodered/configure` | `mode` auto/url、`url`、`username`、`password`、`verifySsl` | 同 status；密碼留空且帳號未變時沿用 |
| `rexlite/nodered/start_addon` | – | 同 status |
| `rexlite/nodered/scan` | `template`、`host`、`port`、`maxUnit`（冷氣 1–32，預設 16） | `devices`（窗簾：address、label、position；冷氣：address、state；電子鎖：address `1`、lock `{lock, door}`）、`existing`（已部署的名稱與房間）、`durationMs` |
| `rexlite/nodered/test` | `template`=窗簾、`host`、`port`、`address`、`action` open/close/stop | `ok` |
| `rexlite/nodered/deploy` | `operationId`（UUID v4）、`bridge`、`flow`、`broker` | 背景工作 |
| `rexlite/nodered/job` | `jobId` | 背景工作 |
| `rexlite/nodered/remove` | `operationId`、`bridgeId` | 背景工作 |

背景工作：`state` running / completed / failed / interrupted，`stage` connect → modules → deploy → verify，`error {code, message}`。同一 `operationId` 重送回傳同一工作；同時只執行一個（`node_red_busy`）。主機重新啟動會標為 `interrupted`，以 `status` 重新確認。

錯誤碼以 `node_red_` 開頭，`message` 為可直接顯示的繁體中文。

## 部署規則

- `bridge`：`template`（`hitachi_ac_modbus` / `somfy_curtain_rs485` / `yale_lock_ya071`）、`gatewayHost`（RFC1918 IPv4）、`gatewayPort`、`devices`（1–32 台；`name`、`key` 小寫房間代號、`address` 站號 1–247、6 碼馬達 ID，或電子鎖固定 `1` 且每個閘道埠只能一把）。
- `flow` 只能有一個 `tab`（16 碼 hex、標籤以 `RexLite` 開頭），節點類型限 inject、function、delay、tcp request、mqtt in/out、debug、catch、comment、modbus-flex-getter/write；設定節點限 `rexlite_mqtt_broker` 與 16 碼 hex 的 modbus-client。閘道 IP／Port 必須與 `bridge` 相同；MQTT Topic 限 `ac/`、`curtain/`、`lock/`、`rexlite/nodered/`；不可帶 `credentials`。
- 合併：移除同一分頁、其節點、本次與前次的閘道設定節點後加入新流程，其餘流程原樣保留；以 v2 `rev` 部署（`Node-RED-Deployment-Type: flows`），衝突重試一次。
- 需要 modbus 節點時自動安裝 `node-red-contrib-modbus`（案場需可連網）。
- `broker`：`mode` auto（依 HA MQTT 設定；add-on 使用 host network 而 HA 指向 `core-mosquitto` 等主機名稱時，改用 Broker add-on 對外埠 `127.0.0.1:<port>`）或 custom（`host`、`port`）；`useHaCredentials` 預設使用 HA 的 MQTT 帳密，於主機端寫入 Node-RED 憑證，不經雲端。

## 心跳

流程每 10 秒（啟動後 1 秒先發一次）以 retain 發佈 `rexlite/nodered/<bridgeId>/status`：

```json
{"deployment": "<operationId>", "ts": 1790000000000, "gateway": {"lastRxAgeMs": 1200}}
```

部署最多等待 35 秒：`deployment` 相符且 `ts` 在 2 分鐘內代表 Node-RED 已連上 MQTT；`lastRxAgeMs` < 30 秒代表閘道有回應。流程可帶 `gateway.pollMs`（例如電子鎖每 60 秒查詢一次以節省電池），此時門檻為 1.5 個週期，上限 10 分鐘。移除時清除 retained 訊息。

## 協定

- Somfy SDN：位元反相傳送，最後 2 bytes 為原始位元組 16-bit 加總。NodeType F6h（主控 → Glydea）。GET_NODE_ADDR 40h 廣播 FFFFFFh，重複 3 輪並交替 ACK 位元收集 POST_NODE_ADDR 60h；之後逐一讀 GET_NODE_LABEL 45h 與 GET_MOTOR_POSITION 0Ch。POST_MOTOR_POSITION 第 11 byte 為開度（0 = 上限），FFh 代表未知；HA 位置 = 100 − 開度。
- 日立：Modbus TCP 功能碼 03 讀 0x0040 起 4 個暫存器（電源、模式、風速、室溫）；站號 1–247。
- Yale YA071（v1.6）：`05 ID CMD DATA CRC 0F`，ID 91h 控制器 → 模組、19h 模組 → 控制器；DATA 首 byte 高 4 bit 為 DATA 長度；CRC = ID ⊕ CMD ⊕ 全部 DATA。狀態回覆 `05 19 01 21 ST`：ST 高 4 bit 為上鎖（1 未鎖、2 已鎖），低 4 bit 為門磁（1 開、2 關、0 無資料）。模組主動事件（81h–8Eh）須在 2–50 ms 內以 ID 91h 原樣回 ACK，由 Node-RED 流程處理。

## 驗證

`python3 -m unittest tests.test_rs485_protocol tests.test_node_red_client tests.test_node_red_bridge`。協定測試比對廠商 28 組窗簾指令、50% 開度指令與 3 筆實際位置回覆，並以本機模擬閘道執行掃描。實機 RS-485 匯流排與 Node-RED add-on ingress 須於案場驗收。
