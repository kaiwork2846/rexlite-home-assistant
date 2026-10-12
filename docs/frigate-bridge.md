# Frigate 攝影機橋接

運營平台「部署設定 → 攝影機 → ＋」與 MAX'Is Tool App「攝影機配對」使用的主機端指令：把 IP 攝影機加入與 HA 同一台主機上的 Frigate。全部為管理員 WebSocket 指令，經 HA MCP → Go core → tunnel 呼叫；本套件不開放任何新連接埠。

## 連線

- Frigate 內部 API：依序嘗試 `http://127.0.0.1:5050`（IPC 安裝器的 `5050:5000`）、`http://127.0.0.1:5000`。HA 以 host 網路執行，所以可直接連到 Frigate 發佈的連接埠。
- go2rtc：`http://127.0.0.1:1984`。連不上時改由 Frigate 直接讀攝影機（不建 restream）。
- 攝影機只接受 RFC1918 IPv4；串流網址必須指向同一台攝影機，不得含帳密。

## 指令

| type | 參數 | 回傳 |
| --- | --- | --- |
| `rexlite/frigate/status` | – | `frigate`（reachable、version、uptime、restream）、`cameras`（camera、label、host、enabled、record、detect、ptz、fps、online、managed、placeholder）、`job` |
| `rexlite/frigate/discover` | `seconds`（1–8，預設 3） | `cameras`（host、port、name、hardware、vendor、configured）。在每個區網介面送 ONVIF WS-Discovery；只收宣告 `NetworkVideoTransmitter` 或 ONVIF scope 的回應（排除 Windows／NAS 的 WSD :5357） |
| `rexlite/frigate/probe` | `host`、`port`、`username`、`password`、`rtspUrl`、`hint` | `device`、`vendor`、`onvif`、`streams`（uri、codec、width、height、ok、error）、`main`、`sub`、`existing`、`suggestedName`、`hints` |
| `rexlite/frigate/add` | `operationId`（UUID v4）、`name`、`host`、`main`、`sub`、帳密、`mainWidth/Height`、`subWidth/Height`、`record`、`detect`、`ptz`、`onvifPort` | 背景工作 |
| `rexlite/frigate/job` | `jobId` | 背景工作 |
| `rexlite/frigate/remove` | `operationId`、`camera` | 背景工作 |
| `rexlite/frigate/snapshot` | `camera`、`height`（120–720） | base64 JPEG |

### probe

1. ONVIF：先試輸入的埠，再試 2020（Tapo、VIGI Wi-Fi）、80、8000、8899。以攝影機自己的時鐘簽 WS-Security（時間錯誤的攝影機也能驗證），必要時改用 HTTP Digest。取得型號、各 Media Profile 的 RTSP 網址（主機改為實際連線的 IP）。
2. 每條串流以 RTSP DESCRIBE 驗證：在同一條連線回應 Digest 挑戰（gortsplib 類攝影機的 nonce 綁連線）；攝影機回覆後即斷線時，換新連線重送。
3. ONVIF 不可用時，依廠牌試常見路徑（TP-Link `/stream1`、`/stream2` 優先，再試 Hikvision、Dahua、Reolink）。
4. ONVIF 沒提供解析度時，請 Frigate 的 `/api/ffprobe` 補上。
5. 錄影選最大的 H.264／H.265 串流，偵測選寬度 ≥ 640 的最小串流。

### add

`streams`（DESCRIBE）→ `configure`（go2rtc restream、`PUT /api/config/set`）→ `restart`（`POST /api/restart`）→ `verify`（`camera_fps > 0`）。失敗時 `rollback`。同一 `operationId` 重送回傳同一工作；同時只執行一個（`frigate_busy`）。HA 重新啟動時執行中的工作標為 `interrupted`。

- Frigate 範例攝影機 `name_of_your_camera`（路徑 `rtsp://10.0.10.10:554/rtsp`）會一併移除。
- go2rtc 串流網址的帳密以百分比編碼；Frigate 自己的 ffmpeg／ffprobe 路徑（無 go2rtc 時）用原始帳密，因為 Frigate 會再編碼一次。
- 帳密只寫入主機上的 Frigate 設定；不寫入 HA 儲存、工作紀錄、結果或錯誤訊息。

### 為何加入與移除都重新啟動 Frigate

Frigate 0.16–0.17（含 0.17.1）的即時新增／移除不可靠，已以 Frigate 0.17.1 容器實測：

- `CameraConfigUpdateSubscriber` 在 Frigate 啟動時只有一台攝影機（新主機的範例攝影機，或第一台真實攝影機）時，只訂閱該攝影機的主題，收不到新攝影機的 `add`。物件處理執行緒因 `KeyError` 停止，錄影維護每 5 秒失敗，所有攝影機的錄影都存不下來。
- 即時 `remove` 會停掉「最後載入」的攝影機，而不是指定的那台（`frigate/camera/maintainer.py`）。

移除前先以即時更新關閉該攝影機的錄影與擷取，等 15 秒讓 Frigate 丟棄它的錄影快取片段，再刪除設定並重啟：已移除攝影機殘留在 `/tmp/cache` 的片段會讓錄影維護持續失敗（`frigate/record/maintainer.py`），影響其他攝影機。

## 錯誤碼

`message` 為可直接顯示的繁體中文。`camera_*`：`camera_invalid_request`、`camera_unreachable`、`camera_auth_failed`、`camera_onvif_failed`、`camera_stream_not_found`、`camera_rtsp_failed`、`camera_discovery_failed`。`frigate_*`：`frigate_unreachable`、`frigate_unsupported`、`frigate_busy`、`frigate_camera_exists`、`frigate_codec_unsupported`、`frigate_config_rejected`、`frigate_restream_failed`、`frigate_stream_not_running`、`frigate_camera_not_found`、`frigate_job_not_found`、`frigate_snapshot_failed`、`frigate_interrupted`、`frigate_failed`。
