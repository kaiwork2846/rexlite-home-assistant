# Frigate 攝影機橋接

運營平台「部署設定 → 攝影機 → ＋」與 MAX'Is Tool App「攝影機配對」使用的主機端指令：把 IP 攝影機加入與 HA 同一台主機上的 Frigate。全部為管理員 WebSocket 指令，經 HA MCP → Go core → tunnel 呼叫；本套件不開放任何新連接埠。

## 連線

- Frigate 內部 API：依序嘗試 `http://127.0.0.1:5050`（IPC 安裝器的 `5050:5000`）、`http://127.0.0.1:5000`。HA 以 host 網路執行，所以可直接連到 Frigate 發佈的連接埠。
- go2rtc：`http://127.0.0.1:1984`。連不上時改由 Frigate 直接讀攝影機（不建 restream）。
- 攝影機只接受 RFC1918 IPv4；串流網址必須指向同一台攝影機，不得含帳密。

## 指令

| type | 參數 | 回傳 |
| --- | --- | --- |
| `rexlite/frigate/status` | – | `frigate`（reachable、version、uptime、restream）、`cameras`（camera、label、host、enabled、record、detect、recordDays、eventDays、objects、ptz、fps、online、managed、placeholder）、`job` |
| `rexlite/frigate/discover` | `seconds`（1–8，預設 3） | `cameras`（host、port、name、hardware、vendor、configured、uninitialized）。在每個區網介面送 ONVIF WS-Discovery；只收宣告 `NetworkVideoTransmitter` 或 ONVIF scope 的回應（排除 Windows／NAS 的 WSD :5357） |
| `rexlite/frigate/probe` | `host`、`port`、`username`、`password`、`rtspUrl`、`hint` | `device`、`vendor`、`onvif`、`streams`（uri、codec、width、height、ok、error）、`main`、`sub`、`existing`、`suggestedName`、`hints` |
| `rexlite/frigate/activate` | `host`、`port`（ONVIF，預設 80）、`password`（9–32 字元） | `host`、`username`（`admin`）、`activated`、`clock` |
| `rexlite/frigate/add` | `operationId`（UUID v4）、`name`、`host`、`main`、`sub`、帳密、`mainWidth/Height`、`subWidth/Height`、`record`、`detect`、`ptz`、`onvifPort` | 背景工作 |
| `rexlite/frigate/job` | `jobId` | 背景工作 |
| `rexlite/frigate/remove` | `operationId`、`camera` | 背景工作 |
| `rexlite/frigate/camera_info` | `camera` | 攝影機本身的設定（ONVIF）：`host`、`port`、`ptz`（`presets`）、`image`（brightness、saturation、contrast、sharpness 的 value／min／max，`irCut`）、`clock`（ntp、server、timeZone、cameraTime、driftSeconds）；不支援的區段為 `null` |
| `rexlite/frigate/webrtc` | `camera`、`offer`（瀏覽器 SDP，含全部 ICE candidates）、`quality`（`main`／`sub`，預設 `sub`）、`talk` | `answer`（go2rtc SDP）、`quality`、`qualities`、`talk`（攝影機接受麥克風聲音） |
| `rexlite/frigate/camera_control` | `camera`、`op`（`move`、`stop`、`goto`、`save_preset`、`remove_preset`、`image`、`clock`、`quality`），依 op：`profile`、`quality`（width、height、fps、bitrate）、`direction`（up／down／left／right／zoom_in／zoom_out）、`speed`（0.1–1）、`seconds`（0.1–2）、`token`、`name`、`image`、`ntp`、`server` | 更新後的區段；`move`／`goto` 附一張移動後的畫面（base64 JPEG） |
| `rexlite/frigate/configure` | `operationId`、`camera`，以及至少一項：`record`、`detect`、`recordDays`、`eventDays`（0–365）、`objects`（`person`、`car`、`motorcycle`、`bicycle`、`dog`、`cat`，至少一項） | 背景工作 |
| `rexlite/frigate/snapshot` | `camera`、`height`（120–720） | base64 JPEG |

### probe

1. ONVIF：先試輸入的埠，再試 2020（Tapo、VIGI Wi-Fi）、80、8000、8899。以攝影機自己的時鐘簽 WS-Security（時間錯誤的攝影機也能驗證），必要時改用 HTTP Digest。取得型號、各 Media Profile 的 RTSP 網址（主機改為實際連線的 IP）。
2. 每條串流以 RTSP DESCRIBE 驗證：在同一條連線回應 Digest 挑戰（gortsplib 類攝影機的 nonce 綁連線）；攝影機回覆後即斷線時，換新連線重送。
3. ONVIF 不可用時，依廠牌試常見路徑（TP-Link `/stream1`、`/stream2` 優先，再試 Hikvision、Dahua、Reolink）。
4. ONVIF 沒提供解析度時，請 Frigate 的 `/api/ffprobe` 補上。
5. 錄影選最大的 H.264／H.265 串流，偵測選寬度 ≥ 640 的最小串流。

### activate（VIGI 開通）

全新或重置的 TP-Link VIGI 攝影機會回應 ONVIF 搜尋，但在網頁設定 `admin` 密碼前拒絕所有登入。中樞代替攝影機網頁送出同樣的匿名請求，安裝人員不必開攝影機網頁：

1. 偵測：`POST https://IP/` `{"method":"do","user_management":{"get_encrypt_info":null}}`，`data.code` 為 `-40405` 即未開通。`discover` 只對未加入的 TP-Link 攝影機查詢並標 `uninitialized`；`probe` 帳密錯誤時也會查，未開通回 `camera_uninitialized`。攝影機 HTTPS 偶爾斷線，查詢失敗會重試一次。
2. 開通：`{"method":"do","set_password":{…}}`，`username` 為 `admin`，`ciphertext` 是以攝影機網頁內建 RSA 公鑰（PKCS#1 v1.5）加密後 base64、URL 編碼的密碼；預設三個安全問題、答案留空，語言設繁體中文（攝影機支援時）。
3. 時鐘：以 ONVIF `SetSystemDateAndTime` 寫入中樞時間與時區（`Asia/Taipei` → `UTC-8`），取代攝影機網頁的國家／時區欄位。

密碼規則與攝影機相同：9–32 個可列印字元，含大寫、小寫、數字、符號，不可有 3 個相同或連續字元（`aaa`、`abc`、`123`），不可含 `admin`；不符時不送出（`camera_weak_password`）。密碼只用於這次請求。2026-10-23 以 VIGI C540V（韌體 2.2.0）實測：開通、時區、ONVIF 與 RTSP 主／子串流皆正常。

### add

`streams`（DESCRIBE）→ `configure`（go2rtc restream、`PUT /api/config/set`）→ `restart`（`POST /api/restart`）→ `verify`（`camera_fps > 0`）。失敗時 `rollback`。同一 `operationId` 重送回傳同一工作；同時只執行一個（`frigate_busy`）。HA 重新啟動時執行中的工作標為 `interrupted`。

- Frigate 範例攝影機 `name_of_your_camera`（路徑 `rtsp://10.0.10.10:554/rtsp`）會一併移除。
- 偵測串流一律以軟體解碼（`ffmpeg.hwaccel_args: []`）。Frigate 預設 `auto` 在 Intel 主機會選 VAAPI；2026-10-23 Nick 林口設備的 VIGI C540V 子串流每 30–40 秒解碼失敗一次（`Failed to sync surface`），ffmpeg 重開約 300 次後 Frigate 檔案描述元用盡、看門狗執行緒停止，攝影機一直沒有畫面（平台顯示離線），重開攝影機也不會恢復。偵測串流小、5 fps，軟體解碼負擔很小。要寫 `[]` 而不是 `""`：空字串會讓 `/api/config/set` 刪掉這個鍵，又回到 `auto`。
- go2rtc 串流網址的帳密以百分比編碼；Frigate 自己的 ffmpeg／ffprobe 路徑（無 go2rtc 時）用原始帳密，因為 Frigate 會再編碼一次。
- 帳密只寫入主機上的 Frigate 設定；不寫入 HA 儲存、工作紀錄、結果或錯誤訊息。

### camera_info／camera_control（攝影機本身設定）

- 帳密：主機從本機 Frigate 的 `/api/config/raw` 讀這台攝影機的 `onvif` 帳密，沒有則用它的 go2rtc restream 或 ffmpeg 網址裡的帳密；只用於這次請求，不寫入 HA、不回傳、不出現在錯誤訊息。帳密含 Frigate 環境變數（`{FRIGATE_…}`）時回 `camera_credentials_missing`；攝影機改過密碼回 `camera_auth_failed`（請移除後重新加入）。
- 直接以 ONVIF 連攝影機（先試 Frigate 設定的 `onvif.port`，再試 2020、80、8000、8899），不經 Frigate 的 PTZ：Frigate 只在啟動時讀預置點，新存的預置點要重啟才看得到。
- `move`：主機送 `ContinuousMove`，`seconds` 後一定送 `Stop`；雲端斷線也不會讓攝影機一直轉。同一台攝影機的操作依序執行。
- `goto`：`GotoPreset` 後等 2.5 秒取一張畫面；`move` 等 0.6 秒。畫面取自 Frigate `latest.jpg`（`X-Cache-Bypass`）。
- `save_preset` 不帶 `token` 為新增，帶 `token` 為覆寫（目前位置＋新名稱）。
- `image`：`SetImagingSettings`（`ForcePersistence`），欄位依 ONVIF 順序；`irCut` 為日夜模式：`AUTO`、`ON`（彩色）、`OFF`（黑白／紅外線）。
- `clock`：`ntp: true` 寫入 NTP 伺服器（預設 `time.stdtime.gov.tw`）並改為 NTP 校時；`false` 以主機時間手動對時。兩者都寫入主機時區。

### webrtc（即時影像與通話）

- 只有這一次 SDP 交換經過雲端；影像與聲音由瀏覽器直連主機的 go2rtc（點對點），找不到直連路徑時才經平台的 TURN 轉送。瀏覽器應等 ICE 收集完成再送出 offer（非 trickle）。
- 預設子串流，雲端流量最低；`main` 為主串流。
- 通話：呼叫攝影機的 ONVIF 語音回傳通道（RTSP backchannel）。Frigate 的 restream 都關閉 backchannel（`#backchannel=0`），通話改用 `<camera>_talk`：主機以 Frigate 設定裡的串流網址（含帳密）向 go2rtc 建立（只在主機內），回傳的 `talk` 表示 go2rtc 的 answer 是否接收音訊（攝影機支援 backchannel）。
- `quality`（`camera_control`）：ONVIF Media v1 `SetVideoEncoderConfiguration`，送回完整設定、只改解析度／幀率／位元率，`ForcePersistence`。解析度只能選攝影機 `GetVideoEncoderConfigurationOptions` 回報的值。改子串流解析度後，Frigate 會把偵測串流縮放到原本設定的偵測尺寸。

### configure（攝影機設定）

`recordDays` 是連續錄影保存天數（Frigate 0.16 起 `record.continuous.days`，舊版 `record.retain.days`）；`eventDays` 同時設定警報與偵測事件（`record.alerts.retain.days`、`record.detections.retain.days`）；`objects` 寫入 `objects.track`。`status` 回傳 Frigate 解析後的目前值（未設定時是 Frigate 預設：0.17 為連續 0 天、事件 10 天、只追蹤 `person`）。

- 只寫有變更的欄位，每個區段（`record`、`detect`、`objects`）各以 `update_topic config/cameras/<camera>/<區段>` 即時套用，不重啟。
- 例外：開啟錄影（原本關閉）時寫入後重啟 Frigate 並等畫面。ffmpeg 只有在啟動時錄影已開啟才會輸出錄影片段（`frigate/config/camera/camera.py`）；以 Frigate 0.17.1 容器實測，即時開啟後 40 秒沒有任何錄影檔，重啟後立即開始錄影。
- 寫入後以 `X-Cache-Bypass: 1` 讀回設定：Frigate 的 nginx 會快取 `/api/config` 5 秒。
- Frigate 的 `/api/stats` 中 `detection_enabled` 不會反映 `/api/config/set` 的即時變更（stats 用的是啟動時的設定物件），以 `/api/config` 為準。

### 為何加入與移除都重新啟動 Frigate

Frigate 0.16–0.17（含 0.17.1）的即時新增／移除不可靠，已以 Frigate 0.17.1 容器實測：

- `CameraConfigUpdateSubscriber` 在 Frigate 啟動時只有一台攝影機（新主機的範例攝影機，或第一台真實攝影機）時，只訂閱該攝影機的主題，收不到新攝影機的 `add`。物件處理執行緒因 `KeyError` 停止，錄影維護每 5 秒失敗，所有攝影機的錄影都存不下來。
- 即時 `remove` 會停掉「最後載入」的攝影機，而不是指定的那台（`frigate/camera/maintainer.py`）。

移除前先以即時更新關閉該攝影機的錄影與擷取，等 15 秒讓 Frigate 丟棄它的錄影快取片段，再刪除設定並重啟：已移除攝影機殘留在 `/tmp/cache` 的片段會讓錄影維護持續失敗（`frigate/record/maintainer.py`），影響其他攝影機。

## 錯誤碼

`message` 為可直接顯示的繁體中文。`camera_*`：`camera_invalid_request`、`camera_unreachable`、`camera_auth_failed`、`camera_uninitialized`、`camera_weak_password`、`camera_not_vigi`、`camera_already_active`、`camera_activate_failed`、`camera_onvif_failed`、`camera_stream_not_found`、`camera_rtsp_failed`、`camera_discovery_failed`。`frigate_*`：`frigate_unreachable`、`frigate_unsupported`、`frigate_busy`、`frigate_camera_exists`、`frigate_codec_unsupported`、`frigate_config_rejected`、`frigate_restream_failed`、`frigate_stream_not_running`、`frigate_camera_not_found`、`frigate_job_not_found`、`frigate_snapshot_failed`、`frigate_interrupted`、`frigate_failed`。
