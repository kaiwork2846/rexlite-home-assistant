# Changelog

## 0.1.43

- KNX 情境：GVS CHPBD 按鍵面板應用程式 1.2 的情境鍵也從發送端參數讀場景號。`SCENE_LAYOUTS`／`WALL_KEY_APPLICATIONS` 加入 CHPBD-04 `00BC-12-78B4`、CHPBD-06 `00BD-12-0DBA`（版面同 1.0：`MD-4`、`O-2-0_R-56`、`UP-59_R-62`「8 bit scene number」，已用 D3-7F 匯出專案的應用程式 XML 核對）；CHPBD-02 1.2 `00BB-12-301A` 本來就在表上。REXLiTE ETS App 的 CHPBD-02／04／06 profile 都釘在 1.2，之前這兩款的情境鍵被當成 `missing_explicit_scene_number` 跳過，HA 沒有 scene。D3-7F 專案：0/7/4～0/7/6（CHPBD-06 按鈕-4～6）多出 3 個 scene，skipped 只剩 `function_role_not_exposed`。
- 跟 0.1.29 一樣，新的情境只在**重新上傳** `.knxproj` 時生效（場景號在上傳解析時讀進存起來的專案，mapper 只讀那份）；`MAPPER_REVISION` 不變（仍是 12），升版也不會讓已匯入的專案多出 scene。

## 0.1.42

- OpenAPI 開不起來時改走攝影機網頁介面：一鍵設定過的攝影機，`vigi_control` 的開閘（`alarm_output.lpr_manual_alarm`）、辨識觸發（`lpr_para.reg_video`、`alarm_input.notify_list_AI1.lpss_enabled`）與 `lpr_records`（`system.get_user_id` + `license_plate_recognition.search_license_plate`）在 OpenAPI 連不上時自動改用攝影機網頁 API；車牌圖片仍需 OpenAPI，連不上時回傳明確原因。`vigi_info` 加上 `web`（可改走網頁介面）。
- 一鍵設定的 OpenAPI 步驟：開啟後最多等約 30 秒；失敗時讀回攝影機的 OpenAPI 開關，分辨「攝影機沒有套用 OpenAPI 開關」與「已開啟但連不上（20443 埠拒絕連線／沒有回應／加密連線失敗）」。2026-10-29 科盛 InSight LPR345Z 實機：登入、車牌辨識、推送、開閘連動、名單同步皆成功，只有 OpenAPI 開啟後 8 秒內仍連不上。
- OpenAPI 加密連線失敗另回 `camera_openapi_tls`（附原因），不再一律視為未開啟。

## 0.1.41

- 新增 `rexlite/frigate/vigi_setup`（admin）：VIGI／InSight 攝影機一鍵設定，不必再登入攝影機網頁。以 Frigate 設定裡的帳密走攝影機網頁本身的 JSON API（`vigi_web`；請求格式取自 TP-Link 公開的 InSight LPR345Z 網頁模擬器，登入方式與 VIGICam 專案的實機記錄一致）：開啟 OpenAPI 並等它回應；LPR 機型再開啟車牌辨識（影像觸發）、把 Alarm Server 指向中樞的 webhook、設定開閘連動（只有白名單車牌輸出繼電器）、合併並同步白／黑名單。每一步各自回報結果，登入失敗不做任何變更。
- 白／黑名單存進攝影機：第一次設定先把攝影機上平台沒有的車牌併入平台名單，之後以平台為準，每次儲存名單都在背景同步到攝影機（新增、修改、刪除；進度在 `listSync`）。攝影機自己比對名單、自己開閘，中樞離線也能進出；中樞收到該車牌只記錄為 `camera`，不再送第二次開閘訊號。
- `vigi_control` 的 `auto_gate`：攝影機自己開閘時，改的是攝影機的開閘連動。
- `vigi_info.plates`、`lpr_list.cameras` 加上 `cameraGate`、`cameraList`、`listSync`、`setupAt`；`lpr_list_save` 回傳 `syncing`。
- 尚未以 LPR345Z 實機驗證；失敗的步驟仍可在攝影機網頁手動設定。

## 0.1.40

- VIGI 一鍵開通支援 InSight 機型（如 InSight LPR345Z）：先走官方 OpenAPI 初始化（VIGI IPC Open API Document V1.3 §2.4：`https://<ip>/openapi/initialize` 的 `getEncrptInfo` 取得攝影機自己的 RSA 金鑰，再 `setPassword`），不支援時才用原本攝影機網頁的 `set_password`（VIGI C540V）。網頁 API 沒有回應開通狀態的攝影機，以 OpenAPI 初始化是否開放判斷是否為全新／重置。`activate` 回傳 `method`（`openapi`／`web`）。
- 開通密碼規則同時符合兩種方式：10–32 個可列印字元，含大寫、小寫、數字、符號，不可有 2 個相同的連續字元或 3 個連續字元（`abc`、`123`），不可含 `admin`。
- 中樞記住攝影機密碼（HA 私有儲存 `.storage/rexlite.frigate`，以 MAC 為鍵，IP 變更仍認得）：一鍵開通設定的密碼、加入成功的帳密、移除攝影機時 Frigate 設定裡的帳密都會記住。`discover` 對已記住密碼的攝影機標出 `remembered`（並以中樞 ARP 表補上 `mac`）；`probe`、`add` 沒有帶帳密時自動使用記住的密碼，重置後開通、移除後重新加入都不必再輸入。結果與錯誤訊息仍不含密碼。
- 新增 `rexlite/frigate/vigi_password`（admin）：管理員查看攝影機密碼（例如登入攝影機網頁開啟 OpenAPI、設定 Alarm Server），依 `camera`（Frigate 攝影機）或 `host`。

## 0.1.39

- 新增 TP-Link VIGI／InSight 攝影機的原廠 OpenAPI（官方「VIGI IPC Open API Document」V1.3，InSight LPR345Z 版加上車牌功能）：`rexlite/frigate/vigi_info`、`rexlite/frigate/vigi_control`（admin）。讀取型號／韌體、日夜模式（自動／白天／夜間）、主／子串流編碼（H.264、H.265、H.264+、H.265+）、microSD 狀態；可修改日夜模式與編碼。攝影機需先在網頁「設定 → 網路設定 → OpenAPI」開啟（HTTPS 20443）；沒開啟時回 `openapi.state = "off"` 與開啟說明。認證為官方兩段式 `doAuth`（SHA-256），帳密從本機 Frigate 設定讀取，不回傳、不記錄。
- 車牌辨識（InSight LPR 機型）：遠端開閘（`openBarrierGate`，攝影機繼電器）、辨識觸發方式（畫面／警報輸入，`lprTriggerMode`）、`rexlite/frigate/lpr_records` 查詢攝影機 microSD 上的車牌紀錄（車牌、時間、攝影機名單類別）、`rexlite/frigate/lpr_image` 下載車牌特寫與現場畫面（只回傳要求的那張）。
- 平台車牌名單：攝影機內建的白名單／黑名單沒有 API，名單改存中樞（`rexlite/frigate/lpr_list`、`lpr_list_save`，最多 2000 筆，可設名稱、開始日、到期日、停用）。攝影機的 Alarm Server 指向中樞的 LAN 專用 webhook（`/api/webhook/<id>`，`vigi_info` 回傳要填的主機、埠、URL）；辨識到白名單車牌且該攝影機開啟「自動開閘」時，中樞以 OpenAPI 開閘；黑名單只記錄。每次判斷保留最近 200 筆並觸發 `rexlite_lpr` 事件供自動化使用；同一台車 8 秒內重複回報只處理一次。
- `camera_info` 的 `ptz` 加上 `panTilt`、`zoom`（ONVIF `GetNodes`），只能變焦的槍型攝影機（如 InSight LPR345Z）不再顯示方向鍵。
- `manifest.json` 的 `dependencies` 加入 `webhook`。

## 0.1.38

- 新增 `rexlite/frigate/configure`（admin，背景工作）：MAX’Is 運營平台可直接修改已加入攝影機的錄影、偵測、連續錄影保存天數、事件保存天數與偵測物件（人、車、機車、腳踏車、狗、貓）。只寫有變更的欄位，以 Frigate 即時更新套用，不重啟；只有開啟錄影時會重啟 Frigate（約 1 分鐘），因為 ffmpeg 只有在啟動時錄影已開啟才會輸出錄影片段（Frigate 0.17.1 實測）。
- `rexlite/frigate/status` 的每台攝影機加上 `recordDays`、`eventDays`、`objects`。
- 新增 `rexlite/frigate/camera_info`、`rexlite/frigate/camera_control`（admin）：攝影機本身的設定，以 ONVIF 直接操作：PTZ 方向／變焦（主機端移動後一定停止）、預置點（前往、新增、覆寫、刪除）、影像（亮度、飽和度、對比、銳利度、日夜模式）、時間（NTP 自動校時或以主機時間對時）。帳密從本機 Frigate 設定讀取，只用於該次請求，不另外保存、不回傳。
- 讀取 Frigate 畫面（`latest.jpg`）略過 nginx 5 秒快取。
- 攝影機畫質：`camera_info` 回傳主／子串流的解析度、幀率、位元率與攝影機支援的選項；`camera_control` 的 `quality` 修改編碼設定（ONVIF Media）。
- 新增 `rexlite/frigate/webrtc`（admin）：即時影像與通話的 WebRTC 信令，影像點對點直連主機的 go2rtc，不經雲端轉送；通話使用攝影機的 ONVIF 語音回傳通道（另開 `<camera>_talk` restream）。

## 0.1.37

- 修正加入的攝影機過一段時間後一直離線：Frigate 預設自動選擇硬體解碼，在 Intel 主機會用 VAAPI。2026-10-23 Nick 林口設備的 VIGI C540V 子串流每 30–40 秒解碼失敗一次（`Failed to sync surface`），ffmpeg 重開約 300 次後 Frigate 檔案描述元用盡、看門狗執行緒停止，攝影機一直沒有畫面（平台顯示離線），重開攝影機也不會恢復。
- `rexlite/frigate/add` 寫入的攝影機改為偵測串流一律軟體解碼（`ffmpeg.hwaccel_args: []`）；偵測串流小、5 fps，負擔很小。已加入的攝影機不會自動修改。

## 0.1.36

- 新增 `rexlite/thread/border_routers`（admin）：列出主機區網上的 Thread 邊界路由器（HA zeroconf 已在監聽 `_meshcop._udp`，直接讀快取；快取是空的才另外監聽 4 秒），回傳廠牌、型號、Thread 網路名稱、border agent id，以及是否已設定為 Open Thread Border Router 整合。
- 每台路由器只探測私有 IPv4（沒有才用 ULA IPv6）的 OpenThread REST `:8081/node/ba-id`，回傳 `rest`（`ok`／`closed`／`unreachable`）與可直接加入 OTBR 整合的 `restUrl`。GL.iNet GL-S20 等 OpenThread 路由器需先在路由器開放 8081 埠；Apple TV、Google 等不提供 REST，會是 `closed`。
- 搭配 HA MCP `POST /ha/hubs/:hubId/thread/prepare`（自動加入 Thread 與 OTBR 整合，MAX’Is Matter 加入精靈使用）；舊版主機回 `unknown_command`，精靈顯示「Thread：請更新 REXLiTE AI」。
- `manifest.json` 的 `after_dependencies` 加入 `thread`（只讀它的探索工具；沒有設定 Thread 整合時不會先載入）。

## 0.1.35

- 新增 VIGI 攝影機一鍵開通（`rexlite/frigate/activate`，admin）：全新或重置的 TP-Link VIGI 攝影機在網頁設定 `admin` 密碼前拒絕所有登入，MAX’Is 加入攝影機精靈因此卡在登入（2026-10-23 Nick 林口設備的 VIGI-C540V）。中樞改用攝影機網頁相同的匿名 `set_password` 設定密碼（以網頁內建的 RSA 公鑰加密），再以 ONVIF 寫入中樞的時區與時間；安裝人員不必打開攝影機網頁填國家、時區、安全問題。
- `rexlite/frigate/discover` 對還沒加入的 TP-Link 攝影機標出 `uninitialized`；`rexlite/frigate/probe` 遇到未開通的 VIGI 改回 `camera_uninitialized`（原本是 `camera_auth_failed`）。攝影機 HTTPS 偶爾斷線，查詢失敗會重試一次。
- 密碼先依攝影機自己的規則檢查（9–32 個字元，含大寫、小寫、數字、符號，不可有 `aaa`／`abc`／`123`，不可含 `admin`），不符時回 `camera_weak_password`，不會送到攝影機。
- 以 VIGI C540V（韌體 2.2.0）實測：開通、時區、ONVIF 與 RTSP 主／子串流皆正常。

## 0.1.34

- 修正經雲端重新載入 REXLiTE AI 會讓主機一直離線：HA 2026.8 的 HTTP 伺服器在用戶端斷線時會取消處理中的請求（`handler_cancellation`）。重新載入卸載 REXLiTE AI 時會關掉承載這個請求的 Tunnel，HA 就把重新載入取消在半途，設定項目停在 `unload_in_progress`（不可恢復），只能重新啟動 HA。2026-10-22 Nick 林口設備在 MAX’Is 整合商店按「重新載入」兩次，兩次都這樣斷線。
- 經 Tunnel 收到重新載入本設定項目（`POST /api/config/config_entries/entry/<本項目>/reload`）時，先回 `{"require_restart": false}`，1 秒後由 HA 自己的工作重新載入；Tunnel 斷一下就重新連上。
- Tunnel 結束時（重新載入、網路中斷、切換遠端控制），還在等 HA 回應的請求不再被關掉：連線保留到 HA 開始回應（最長 `request_timeout`），HA 不會把進行中的操作（例：`homeassistant.reload_config_entry`、刪除設定項目）取消在半途。呼叫端自己放棄（`stream_close`）仍照舊中止。
- 從雲端關閉「Cloud service」開關：之前停掉 Tunnel 後，重新啟動那一步會跟著請求被取消，主機整台離線；現在重啟由自己的工作完成，主機維持只回報健康狀態的連線。
- 設定項目卸載後，延遲中的重啟不會再把 Tunnel 帶起來。

## 0.1.33

- KNX mapper（`MAPPER_REVISION` 12）：DPT 9.030（μg/m³，PM2.5／PM10）列為量測值，輸出 HA `sensor`（`type: "9.030"`）。之前只收到 9.029，GVS CSMS 多功能感測器（9 合 1）的 PM2.5、PM10 被當成 `insufficient_supported_entity_metadata` 跳過；CO2、TVOC、甲醛（9.008 ppm）本來就會出。REXLiTE KNX Contract 感測量測新增 CO2／PM2.5／PM10／TVOC／甲醛（ETS App 0.36.0）。
- `MAPPER_REVISION` 從 11 升到 12：已匯入的專案下次部署會重新規劃，有 9.030 位址的多出感測器，其他輸出不變。

## 0.1.32

- KNX mapper（`MAPPER_REVISION` 11）：冷氣風速接受 DPT 5.100「fan stage」（0＝自動、1～4＝低／中／高／超高），輸出 HA `fan_speed_mode: step`、`fan_max_step: 4`、`fan_zero_mode: auto`；DPT 5.001 百分比照舊。REXLiTE ETS App 0.35.0 起，GVS BTPG-04/03.1（KNX-485）接大金冷氣時「風速／風速狀態」是 5.100，7TS／3TS 畫面也改送檔位；Contract 與安裝商方言（`FAN`／`FAN-FB`，例：HQ-2F 的大金集控器）都適用。之前這種風速被當成型別不符整個丟掉，冷氣在 HA 沒有風速可調。
- 同一台冷氣的風速命令與回報一個是 5.001、一個是 5.100 時：Contract Function 整個略過（`role_datapoint_type_mismatch`）；安裝商方言只拿掉風速，冷氣照樣產生。5.010（原始計數）仍不接受。
- `MAPPER_REVISION` 從 10 升到 11：已匯入的專案下次部署會重新規劃（有 5.100 風速的冷氣多出風速，其他輸出不變）。

## 0.1.31

- 修正 KNX 部署在 HA 2026.8（及 2026.1～2026.7）一有窗簾按鈕就失敗：HA 的 YAML `button` unique_id 是「位址_payload」（例：`1/3/3_1`，`payload` 預設 1，`value`＋`type` 換算成原始 payload），部署端卻只用位址，按鈕永遠對不上，驗證階段等滿 20 秒後回滾成 `deployment_failed`。D3-7F（HA 2026.8.0、14 燈＋6 按鈕＋2 情境）兩次部署都是這樣失敗。HA 2026.9 起用自訂 unique_id，不受影響。
- 驗證階段逾時改回 `managed_entities_not_loaded`，並在 HA 日誌寫出「N／M 個已載入」與沒載入的實體；其他非預期例外改用 `_LOGGER.exception` 留下 traceback（之前只回 `deployment_failed`、日誌什麼都沒有）。回傳給平台的錯誤碼仍只有代碼，不帶內容。
- 第一次部署失敗回滾後，不再在 `deployment.json` 留一筆沒有 `identityMode` 的失敗記錄（失敗結果照樣在 `last_attempt.json`）。那筆記錄會被當成舊的自訂 ID 部署，讓 HA 2026.8 之後每次匯入都被 `managed_custom_identity_requires_newer_core` 擋下；0.1.30 已留下的這種記錄（沒有 `managedDigest`）現在直接忽略，不必手動刪檔。

## 0.1.30

- 新增 `rexlite/hacs/bind`（admin，見 `docs/hacs-bind.md`）：MAX'Is運營平台用公司的 GitHub 機器帳號權杖直接綁定 HACS，經銷商不必有 GitHub 帳號。建立與 HACS 自己的裝置登入流程相同的設定項目（`token`、`experimental`）；已有設定項目時結束等待中的 reauth，經 HA 卸載後更新權杖再載入（HACS 的 update listener 會繞過 HA 的 setup lock 自行重載，直接更新已載入的設定項目會撞成 failed_unload）。
- 權杖先向 GitHub `/rate_limit` 驗證（無效、達上限、連不上各有錯誤碼）；不寫入日誌、不回傳，只存在 HACS 的設定項目。指令結構只收 `str`，避免 HA 的 invalid_format 訊息把權杖帶回。
- 需要 HA MCP `/ha/hacs` 的 `bind` 與運營平台後端 `/remote-connections/:agentId/hacs`；舊版主機回 `unknown_command`，平台改走 GitHub 登入。

## 0.1.29

- KNX 情境：GVS CHPB／CHPBD 按鍵面板的「Scene control」鍵也從發送端參數讀場景號（`knx_project_metadata`）。CHPB 沒有 module，每顆鍵的情境物件對應自己那一支的 `8 bit scene number`（新表 `BUTTON_SCENE_LAYOUTS`：2／4／6／8 鍵 Secure 版與 8 鍵非 Secure 版；1 鍵與 6 鍵第 5～6 鍵用另一組 ref）；CHPBD 加入 `00BB-12`、`00BC-10`、`00BD-10` 三個版本（同 `00BD-11` 的版面）。
- 牆面按鍵的情境物件只有發送（產品資料把 Write 關掉），位址上沒有 KNX 接收端。發送端**全部**是這些按鍵時，`enrich_project` 標記 `scene_wall_key`，mapper 不再要求位址上有可寫物件，照樣產生 HA `scene`：這種情境是屋主在 HA／MAXIS APP 掛自動化的觸發點（REXLiTE ETS App 0.33.0 起，7TS 方案的 CHPB 鍵可設成情境鍵）。其他情境照舊要有接收端。
- 讀應用程式 XML 改成串流解析（單檔上限 32 MB、合計 128 MB；一樣拒絕 DTD／entity、只收 UTF-8、Id 不可重複）：CHPB 的應用程式每個約 8.5 MB，只把 CHPB 加進表、照原本 8 MB 上限的話，有 CHPB 的專案會整份放棄、連 3TS 的情境都讀不到。某個應用程式證明不了時，現在只有它自己的鍵不出 scene，不再連累其他設備。
- 新的情境只在**重新上傳** `.knxproj` 時生效（場景號在上傳解析時讀）；`MAPPER_REVISION` 不變。用 120 份案場 ETS 專案（98 份可解析）比對，輸出與 0.1.28 完全相同。

## 0.1.28

- 新增 Frigate 攝影機橋接 `rexlite/frigate/*`（見 `docs/frigate-bridge.md`）：運營平台與 MAX'Is Tool App 經主機把 IP 攝影機加入同機的 Frigate。主機端做 ONVIF WS-Discovery（排除 Windows／NAS 的 WSD 主機）、ONVIF 取串流（攝影機時鐘簽章、2020／80／8000／8899 埠）、RTSP DESCRIBE 驗證帳密與路徑（TP-Link `/stream1`、`/stream2` 等常見路徑備援），再寫入 go2rtc restream 與攝影機設定、重新啟動 Frigate，確認有畫面才算完成，否則還原。
- 加入與移除都以重新啟動套用：Frigate 0.16–0.17 只有一台攝影機時收不到即時新增，物件處理會停止、錄影存不下來；即時移除會停錯攝影機。移除前先停掉該攝影機的錄影與擷取，避免殘留的錄影快取讓其他攝影機的錄影維護失敗。
- 需要 HA MCP 與運營平台後端的 `/ha/frigate`、`/remote-connections/:agentId/frigate`。
- KNX：ETS 專案密碼錯誤時在啟用 KNX 整合前擋下（75d8774）。
- KNX 部署：SI 在平台選「哪些 ETS 情境（REXLiTE 3TS 下方的情境鍵）進 HA」。所有鍵照舊都在 `.knxproj` 裡；新指令 `rexlite/knx/scene_selection`（admin）帶 `excluded: [{address, sceneNumber}]`，沒選的 scene 不寫進產生的 knx YAML、`skipped` 回報 `scene_not_selected`，並立刻重新部署。
- 選擇存在部署 manifest（`sceneSelection`），重試、mapper 升版、同一戶重新上傳都沿用；部署失敗回滾時選擇一起還原。部署結果與 `project_deployment_status` 多回 `sceneKeys`（每個 ETS scene 的名稱、位址、場景號、`selected`），平台據此顯示勾選。`project_capabilities` 多 `sceneSelection: true`。
- 排除的 scene 跟其他 skipped 一樣讓狀態成為 `partial`（雲端驗證規定 `completed` 不能有 skipped）。

## 0.1.27

- KNX mapper（`MAPPER_REVISION` 10）：乾接點窗簾 `CurtainImpulse` 的「關脈衝」也接受 DPT 1.008（Down＝1）。REXLiTE ETS App 0.18.3 起，KAA 關通道直接聽牆面「上下」位址（按「關」本來就送 1），7TS 不再為「關」花一個 LOGIC，一台 7TS 從 2 個窗簾變成 4 個；同一個物件只能有一種 DPT，所以「關脈衝」改宣告為 1.008。原本 1.001 的專案（App 0.14–0.18.2）照舊產生「關」按鈕。
- App 0.18.3 以後匯出的窗簾**需要本版**；舊版 mapper（rev 9）對 1.008 的「關脈衝」回報 `role_datapoint_type_mismatch`，整個窗簾不出實體（已用 App 0.18.3 的 e2e 匯出實測）。

## 0.1.26

- KNX mapper（`MAPPER_REVISION` 9）：REXLiTE KNX Contract 乾接點窗簾 `CurtainImpulse` 的 Function 可以多帶兩個牆面按鍵位址——上下（DPT 1.008，Role `MoveUpDown`）與停止／微調（DPT 1.007，Role `StopStepUpDown`）。GVS CHPB 窗簾鍵與 7TS 畫面按鈕送到這兩個位址，再由 7TS LOGIC 轉成脈衝位址的 1。這兩個位址回報為 `function_role_not_exposed`，不再讓整個窗簾變成 `incompatible_function_roles`；後續的名稱／DPT 分組也不再重新解析它們（否則「停止」會被當成 1.010 而回報型別不符）。三個脈衝 `button` 實體不變。
- REXLiTE ETS App 0.18.1 起匯出的窗簾帶有這兩個位址，**需要本版**；舊版 mapper 會讓這類窗簾整個不出實體。

## 0.1.25

- Node-RED 橋接的 MQTT Broker 自動建議：HA Container（host 網路）的 Broker 設為 `localhost`／`127.0.0.1` 時，本機 Node-RED（IPC 安裝器的 `ha-node-red` 容器，Docker 預設 bridge 網路）連 `localhost` 只會連到容器自己。此時改建議 Docker 主機閘道 `172.17.0.1`（同一連接埠，`source: docker-host`）。0.1.24 以前以 `localhost` 部署的流程心跳不會回報，需重新部署。

## 0.1.24

- Node-RED 橋接新增 Yale／GATEMAN 電子鎖範本 `yale_lock_ya071`（YA071 RF Link Module，RS-232 19200 8N1，經透傳 TCP 序列伺服器）。每個序列埠一把鎖，設備位址固定為 `1`。
- `rexlite/nodered/scan` 對電子鎖送出狀態查詢（`05 91 01 11 81 0F`），回報上鎖與門磁狀態；RF 模組回 `FEh` 時以 `node_red_device_unreachable` 區分「閘道正常、鎖沒有回應」。
- 流程允許 `lock/` MQTT Topic；心跳可帶 `gateway.pollMs`，閘道新鮮度放寬為 1.5 個輪詢週期（上限 10 分鐘），讓每分鐘查詢一次的電子鎖不會被誤判為離線。
- `rexlite/nodered/status` 回傳 `templates`，平台據此判斷此版本可部署的設備類型。

## 0.1.23

- Declare `mqtt` in `after_dependencies` for the Node-RED bridge heartbeat check (hassfest). MQTT remains optional; no behaviour change.

## 0.1.22

- KNX mapper (`MAPPER_REVISION` 8): REXLiTE KNX Contract `CurtainImpulse` —
  a dry-contact curtain on a relay actuator in "switch impulse" mode. A Function
  whose members are the `開脈衝`/`關脈衝`/`停脈衝` addresses (DPT 1.001, no
  standard ETS role) becomes three KNX `button` entities (`{name} 開/關/停`, each
  sending 1). HA's KNX cover cannot drive it (it writes 0/1 on one up/down
  address); a partial set is rejected as `incompatible_function_roles`.
- Deployment accepts the `button` platform (identity: `address`).
- Add administrator-only `rexlite/knx/gateway_scan`: a three-second KNXnet/IP
  search (xknx `GatewayScanner`) that lists interfaces and routers with their
  tunnelling/routing capabilities without starting HA's KNX config flow.
- 新增 Node-RED RS-485 橋接部署（`rexlite/nodered/*`，僅限管理員）：自動找到 HA 的 Node-RED add-on（經 Supervisor ingress，免輸入帳密），或使用指定網址與帳號；可一鍵啟動已停止的 add-on。
- 由主機直接掃描 RS-485 閘道：Somfy SDN 廣播 GET_NODE_ADDR 找出馬達並讀取名稱與位置；日立冷氣經 Modbus TCP 逐一詢問站號。支援以開／停／關辨識窗簾。
- 部署只接受平台產生的單一閘道分頁（節點類型、閘道 IP、MQTT Topic 皆受限制），以 Admin API v2 rev 合併，不影響案場其他流程；缺少的 `node-red-contrib-modbus` 會自動安裝。
- MQTT Broker 帳密在主機端由 HA 的 MQTT 設定填入，不經雲端；部署後以流程的 MQTT 心跳確認 Node-RED 與閘道皆已連線。

## 0.1.21

- Read Deco tracker values with live HA report timestamps so unchanged online nodes and zero-traffic clients do not incorrectly expire.
- Preserve timestamp-based expiry for stopped reporting; do not refresh timestamps merely when diagnostics are requested.

## 0.1.20

- 待加入設備只回傳通過本次 LAN 服務連線檢查的 SSDP / mDNS 項目；不再將歷史探索紀錄、USB 或未知狀態當成在線網路設備。
- 連線檢查限制於探索封包提供的私有 IP 與服務埠，限制併發、總時間並自動關閉連線；不變更或刪除 HA 原有設定流程。
- 回傳在線確認時間，供介面過期後隱藏及同步計數。

## 0.1.19

- Prevent stale SSDP cache entries from blocking active device searches.
  Bound gateway callback setup independently, and keep native SSDP/mDNS scans
  running when optional router identification is slow.
- Verify discovery against HA 2026.6.0 and later published releases in CI.

## 0.1.18

- Add administrator-only, bounded LAN discovery through `rexlite/discovery/scan`.
  Refresh SSDP and mDNS from the HA host and return native pending setup flows.
- Identify unconfigured TP-Link Deco routers from fresh SSDP responses and
  suggest their local management address; keep existing Deco hubs out of pending
  results. Discovery never submits credentials or installs integrations.
- Coalesce concurrent scans, limit their duration and release temporary listeners.
  Powered devices must be network reachable and advertise a supported protocol.

## 0.1.17

- Add Fortinet as a managed HA traffic source provider. Interface SNMP
  counters remain interface-scoped and are never presented as per-client traffic.

- Fix the cloud-service device's firmware label, which remained at 0.1.13
  after installing newer releases. Add a release regression check to keep
  the device version and integration manifest in sync.
- Clarify that installing REXLiTE does not install or authenticate a router
  integration. Per-client traffic needs the router's HA integration or an
  explicitly configured network traffic source. Restart HA after updating.

## 0.1.16

- Add administrator-managed network traffic sources for existing Home Assistant
  rate entities/attributes and SNMP byte counters. Provider labels include
  TP-Link, VIGI, Deco, Zyxel, UniFi and generic devices; actual support depends
  on the device model, firmware and installed HA source integration.
- Expose a read-only, versioned `rexlite/network_traffic` WebSocket response
  for the operations backend. Keep device, interface and router scopes separate;
  credentials and arbitrary source attributes never enter this response.
- Persist source configuration locally through administrator-only actions.
  Protect against failed writes, stale readings, counter resets, reboots and
  duplicate samples. Counter rates are sampling-interval averages.
- Add real HA service, storage and WebSocket checks to the HA 2026.1–2026.9
  compatibility matrix. See `docs/network-traffic.md` for setup examples.
- Includes the previously merged KNX improvements documented in 0.1.13–0.1.15.
  Upgrading requires a full Home Assistant restart; network sources must be
  configured separately and the corresponding operations backend/frontend
  must support the new response.

## 0.1.15

- Fix: the installer `<unit>-Climate-*` dialect added in 0.1.14 emitted
  `climate` rows without `temperature_address` /
  `target_temperature_state_address`. Home Assistant's KNX schema requires
  both, so a single such row made the whole deployment fail schema
  validation (reproduced on a real 25-project corpus with HA 2026.9.1). The
  dialect now also recognises `-Climate-VAL` (DPT 9.001 setpoint),
  `-Climate-VAL-FB` (setpoint feedback) and `-Climate-VAL-REAL-FB` (room
  temperature) and only emits a climate entity when both required
  temperature addresses are proven on the same channel; otherwise the
  command addresses keep their plain fallbacks.
- Map ETS Function members that have no standard ETS DatapointRole by the
  exact `<Function name> <suffix>` convention (prefix must equal the Function
  name, exact DPT still required). This supports colour temperature
  (`色溫`/`色溫狀態`, DPT 7.600 absolute or 5.001 relative), absolute blind
  position and slat angle, and air-conditioner Functions (`FT-0`) with
  on/off, mode, fan speed, setpoint and room temperature.
- Map the standard `HVACMode` role (DPT 20.102) to `operation_mode_address`,
  add `運轉模式`/`運轉模式狀態` labels, and treat heating
  Function types `FT-4`/`FT-5`/`FT-9` as climate.
- Report unmapped Function members as `function_role_not_exposed` (standard
  non-entity roles such as `DimmingControl`) or `unresolved_function_role`
  instead of the generic `insufficient_supported_entity_metadata`.
- Bump the mapping revision (now 7) so imported projects are re-planned.

## 0.1.14

- Split several distinct actuator outputs (e.g. multiple DALI circuits, or
  several air-conditioners) sharing one installer name prefix into their own
  entities by proven actuator channel, instead of refusing the whole
  shared-name group as an ambiguous duplicate command role. Only individually
  distinct, individually-proven channels are split out; a command or optional
  role address without a single proven channel, or two that resolve to the
  same channel, is still never guessed.
- Recognise the installer `<unit>-Climate-SW/-MODE/-FAN(-FB)` convention seen
  on real air-conditioner exports and map it to a `climate` entity built from
  on/off, controller-mode (DPT 20.105) and fan-speed (DPT 5.001) addresses.
  HA's climate schema does not require a temperature pair, unlike the existing
  ETS-Function climate path, so a bare on/off pair with no proven mode or fan
  channel is left as a plain switch rather than a control-less climate entity.
  A non-standard fan/mode datapoint (observed as a raw byte-count 5.010 on
  some real exports, inconsistent with the 5.001 percentage HA expects) is
  left unmapped rather than guessed.
- Bump the mapping revision (now 6) so an already-imported project is
  re-planned on upgrade.

## 0.1.13

- Recognise the `Command` / `Status` (and `Brightness Command` /
  `Brightness Status`, plus the Chinese `指令` / `亮度指令`) group-address
  label convention alongside `開關` / `狀態`, so a dimmable channel exported
  with those labels maps to a single `light` instead of an unpaired `switch`,
  `binary_sensor` and `sensor` with the brightness-command address dropped.
  The exact-datapoint-type guard is unchanged: a `- Command` label that does
  not carry a 1.001 datapoint still blocks the whole name prefix rather than
  mismapping it.
- Bump the mapping revision (now 5) so an already-imported project is
  re-planned on upgrade.

## 0.1.12

- Include Nick's opt-in, reversible HACS 2.0.5 REXLiTE icon repair tool.
  It verifies the official frontend files, preserves the original brand icon,
  updates gzip copies and backs up files for restoration. Run it explicitly
  on the target host; updating the integration does not apply this repair.
- Recognise the `Brightness` / `Value` and `Color` / `Colour` group-address
  label spellings as the same abbreviated roles as `VAL` / `CT` (feedback
  variants included). The exact-datapoint-type guard is unchanged, so a
  `Value` address carrying a temperature datapoint (an air-conditioner
  setpoint) is not read as light brightness.
- Treat an abbreviated channel with a proven brightness or colour-temperature
  address as a dimmable `light` even when the terse installer name carries no
  light word; a channel that proves only on/off stays a `switch`.
- Pair a colour-temperature channel that uses DPT 5.001 (relative percentage)
  as well as DPT 7.600 (absolute kelvin), emitting the matching
  `color_temperature_mode`; drop a feedback address that disagrees with the
  command's encoding instead of pairing it.
- Accept a proven on/off feedback address that a DPT 1.002 (Bool) logic-block
  listener also subscribes to, as long as a Switch or State object drives it
  and the same producing actuator channel is proven.
- Map an "…-FB" group address that has an exact 1-bit status datapoint
  (1.001/1.011) and no communication object at all as a read-only
  `binary_sensor`; a writable object on the address still overrides the name.
- Bump the mapping revision (now 4) so reimporting an already-imported
  project applies the new rules. After upgrading and fully restarting Home
  Assistant, upload the original ETS project again in MAX’Is.

## 0.1.11

- Map the shade group-address labels `Close` (DPT 1.008 up/down), `Step`
  (DPT 1.007 step/stop) and `POS` (DPT 5.001 absolute position) to a single
  `cover`, alongside the existing `Up/Down` / `Stop/Step` / `Position`
  convention. The exact-datapoint-type guard still rejects a label that does
  not carry its role's datapoint type, so an impulse-relay `Close`/`Open`
  pair (DPT 1.001) is not turned into a cover.
- Bump the mapping revision so reimporting an already-imported project after
  upgrading and restarting Home Assistant applies the new cover mapping.

## 0.1.10

- Pair exact SW/SW-FB, VAL/VAL-FB and CT/CT-FB addresses using matching
  actuator-channel metadata, DPTs and communication-object flags. Combine
  supported light control and feedback into one entity.
- Resolve compatible switch/status feedback declarations only with proven
  channel evidence; preserve incompatible or incomplete mappings for review.
- Recover explicit scene numbers from verified ETS sender-application parameter
  layouts. Unsupported or protected layouts remain available for manual review.
- Replan previously imported files when the mapping revision changes, while
  preserving manual YAML and transactional recovery.
- Reversibly disable superseded managed entities after verification, preserving
  registry customizations; exclude restored registry ghosts from loaded counts.

## 0.1.9

- Support bounded 100 MiB ETS transfers with chunk retries, owner checks,
  SHA-256 verification and temporary-file cleanup.
- Add validated manual KNX YAML deployment with preflight checks and rollback.
- Preserve compatibility with Home Assistant 2026.1 and newer.

## 0.1.8

- Fix ETS upload failing with `Unknown error` during capability preflight:
  use Home Assistant's official WebSocket administrator decorator for all four
  KNX commands instead of calling a nonexistent connection method.
- Preserve administrator-only access before starting any import or deployment.
- Test the actual Home Assistant WebSocket dispatcher, administrator and denied
  access, schema validation, import errors and retries across the supported
  2026.1+ compatibility matrix.
- Restart Home Assistant after updating to load the corrected command handlers.

## 0.1.7

- Reconnect when the heartbeat worker fails; cancel and await pending requests
  before reconnecting or stopping so old requests cannot cross cloud sessions.
- Bound cloud writes, local stream backpressure, and stream shutdown; cover
  recovery and cleanup with real WebSocket fault-injection tests.
- Add administrator-only capability, verified ETS import, YAML deployment, and
  deployment-status WebSocket commands for one-upload KNX provisioning.
- Validate file fingerprints and actual KNX schemas, preserve manual YAML/UI
  entities, and transactionally activate a managed package with rollback.
- Verify effective configuration and registered entity states after reload;
  expose skipped mappings and unavailable state counts separately.
- Support automatic KNX deployment on every stable Core release from 2026.1.0:
  legacy address-format identities on 2026.1-7, stable native identities on
  2026.8, and custom identities for new deployments on 2026.9+.
- Preserve canonical identity metadata across Core upgrades and honor official
  registry migrations. Reject conflicting/duplicate identities and unsafe legacy
  address-format changes before changing YAML. Preserve nested manual RGB GAs.
- Support pre-May tuple KNX entity identifiers and avoid normalizing existing
  KNX schemas twice when preserving manual lights or climate entities.
- Report Core/schema incompatibility in capability preflight; the integration's
  minimum stays Core 2026.1.0.
- Restart Home Assistant after installing this release to register the new
  commands. Entry reload alone does not load newly installed integration code.

## 0.1.6

- Classify rejected service credentials separately from temporary network
  failures, stop permanent retry loops, and start Home Assistant reauthentication.
- Validate stored credentials before runtime setup while preserving Home
  Assistant's bounded setup retry for boot-time network unavailability.
- Distinguish a healthy preconfigured connection awaiting user activation from
  an active cloud service, without changing the stable service identity.
- Preserve the same authenticated agent identity across reboot and DHCP address
  changes, and re-publish the current LAN endpoint after connectivity returns.
- Redact the service identity, activation credential, and local Home Assistant
  address from diagnostics, and clean up runtime tasks during unload.

## 0.1.5

- Detect the current Home Assistant host LAN IPv4 address and report it with
  the stable tunnel agent ID.
- Reject loopback, public, link-local, and Docker bridge addresses so stale
  `127.0.0.1` and `172.17.0.1` values are never published as IPC endpoints.
- Re-publish tunnel metadata when the LAN IP changes, allowing the operation
  platform and mobile app to reconcile a moved IPC without re-pairing.

## 0.1.4

- Add compliant 256x256 and 512x512 local brand icons for supported Home
  Assistant versions.
- Consistently describe the customer-facing feature as REXLiTE AI Cloud
  Service in Traditional Chinese and English.
- Preserve existing configurations by keeping the internal service-setting
  schema unchanged.
- Add regression coverage for customer-facing cloud-service terminology.

## 0.1.3

- Rename the integration and setup experience to REXLiTE AI.
- Simplify activation by using the managed REXLiTE gateway and local Home
  Assistant defaults.
- Add local brand assets for Home Assistant 2026.3 and newer.
- Strengthen product-copy, JSON, brand-asset, and release validation checks.

## 0.1.2

- Recreate the authenticated tunnel session whenever a user enables or disables remote administration.
- Close all requests and streams from the previous access mode before reconnecting.
- Preserve health-only monitoring while remote administration is disabled.

## 0.1.1

- Fix dedicated-host Home Assistant pages remaining in a loading state.
- Close completed HTTP streams using RFC-compatible `HEAD`, `Content-Length`, and chunked response framing while preserving WebSocket upgrades.
- Prevent completed remote page requests from accumulating as active tunnel streams.

## 0.1.0

- Add HACS installation support for the REXLiTE Home Assistant integration.
- Support Home Assistant OS, Supervised, Container, and Core installations.
- Add connection status, automatic recovery, and user-manageable settings.
- Add Traditional Chinese and English localization.
- Add automated validation and release checks.
