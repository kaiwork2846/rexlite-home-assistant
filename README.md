# REXLiTE AI for Home Assistant

<p align="center">
  <img src="assets/logo.png" width="250" height="100" alt="REXLiTE logo">
</p>

**目前版本：`0.1.43`**（每版變更見 [CHANGELOG.md](CHANGELOG.md)；HACS 依 GitHub Release 標籤 `vX.Y.Z` 更新）

REXLiTE AI 為居家與場域提供安全、穩定的雲端服務。完成啟用後，使用者可依需求隨時開啟或關閉雲端服務，並持續掌握服務狀態。

## 安裝

安裝前請先完成 [HACS](https://www.hacs.xyz/docs/use/download/download/) 設定。

[![在 HACS 開啟 REXLiTE repository](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=rexliteai&repository=rexlite-home-assistant&category=integration)

若按鈕無法自動開啟：

1. 開啟 **HACS**。
2. 右上角選單選擇 **自訂儲存庫（Custom repositories）**。
3. 輸入 `https://github.com/rexliteai/rexlite-home-assistant`。
4. 類別選擇 **Integration**，按下 **新增**。
5. 開啟 **REXLiTE AI** 並按 **下載**。
6. 重新啟動 Home Assistant。
7. 前往 **設定 > 裝置與服務 > 新增整合**，搜尋 **REXLiTE AI**。
8. 依畫面指示完成設定。

最低支援 Home Assistant `2026.1.0`。若 HACS 已下載但「新增整合」仍找不到 REXLiTE AI，請先重新啟動 Home Assistant，再強制重新整理瀏覽器快取。

## ETS 工程檔自動部署 KNX

REXLiTE AI `0.1.7` 新增單次上傳 ETS 工程檔流程：主機核對檔案內容、解析可判定的實體、建立受管理的 KNX YAML、重新載入 KNX，並確認實體已在 Home Assistant 註冊。MAX’Is 上傳畫面會顯示部署結果；無法判定的位址保留待檢查，不會猜測場景編號或送出控制指令。

此功能支援 Home Assistant **2026.1.0 或更新的正式版本**。系統會依主機版本使用正確的 KNX 實體識別方式：2026.1–2026.7 配合工程檔的群組位址格式、2026.8 使用固定三層位址識別碼、2026.9 起的新部署可使用自訂識別碼。既有部署升級時會沿用官方實體遷移，保留 entity_id；舊版 Core 若工程檔改變全域位址格式，系統會先停止可能造成重複實體的更新並保留既有設定。

請使用 `0.1.8` 或更新版本，修正 `0.1.7` 在 ETS 上傳權限檢查時出現的 `Unknown error`。升級後必須重新啟動 Home Assistant，才會載入修正後的主機指令；只重新載入 REXLiTE 設定項目不會載入新版 Python 程式。安裝完成後，日常 ETS 匯入只需在 MAX’Is 選擇目標主機並上傳檔案，不需要另按安裝 KNX 或編輯 YAML。

原有手寫 KNX YAML 與 include 檔案會保留。產生的內容位於 `/config/.rexlite_knx/entities.yaml`，透過 Home Assistant package 載入；設定檢查或重載失敗時會還原。讀取不到實體狀態與實際控制設備成功是不同狀態，請依介面顯示的可用狀態確認現場連線。管理與相容性細節見 [KNX 自動部署](docs/KNX_AUTO_DEPLOYMENT.md)。

`0.1.10` 改善控制與回饋配對：依完全相同的名稱前綴、執行器通道、DPT 與通訊物件旗標，把 `SW/SW-FB`、`VAL/VAL-FB`、`CT/CT-FB` 合併為燈具。部分已驗證 ETS 應用程式可從明確參數還原場景編號；不支援的版型或資料不完整的位址仍保留手動補齊。升級並重啟 Home Assistant 後，重新上傳原工程檔即可套用新版配對規則。被合併的舊自動實體會保留登錄資料並停用，新實體可在 Home Assistant 中查看。

`0.1.11` 支援窗簾／百葉的 `Close`（DPT 1.008 上下）、`Step`（DPT 1.007 步進/停止）、`POS`（DPT 5.001 絕對位置）標籤，與既有的 `Up/Down`／`Stop/Step`／`Position` 命名並存，合併為單一窗簾實體；DPT 不符的標籤（例如脈衝繼電器式的 `Close`/`Open` DPT 1.001）仍不會被誤判為窗簾。升級並重新啟動 Home Assistant 後，請在 MAX’Is 重新上傳原工程檔，即可套用新版配對規則。

`0.1.12` 依實際工程檔調整燈具配對：`Brightness`/`Value`、`Color`/`Colour` 標籤等同 `VAL`／`CT`（DPT 不符者不受影響，例如冷氣設定溫度的 `Value` 不會被當亮度）；已配對出亮度或色溫的通道即使名稱沒有「燈」字也視為可調光燈具；色溫支援 DPT 5.001（相對百分比）與 7.600（絕對色溫）；狀態位址容忍邏輯模組的 DPT 1.002 訂閱；名稱為 `…-FB` 且無通訊物件的 1-bit 狀態位址視為唯讀感測。升級並重新啟動 Home Assistant 後，請在 MAX’Is 重新上傳原工程檔。

`0.1.12` 亦提供 Nick 的 [HACS 2.0.5 圖示修復工具](docs/brand-icons.md)，可備份及還原原始檔案。此工具需在目標主機明確執行，更新 REXLiTE 整合不會自動套用。

`0.1.13` 支援 `Command`／`Status`（以及 `Brightness Command`／`Brightness Status`、中文 `指令`／`亮度指令`）標籤，與既有的 `開關`／`狀態` 命名並存；以此命名匯出的可調光通道會合併為單一 `light`，而非拆成 `switch`＋`binary_sensor`＋`sensor` 且遺失亮度指令位址。DPT 不符的 `- Command` 標籤仍不會被誤配。升級並重新啟動 Home Assistant 後，請在 MAX’Is 重新上傳原工程檔。

## 支援環境

本整合支援下列 Home Assistant 安裝方式：

- Home Assistant OS
- Home Assistant Supervised
- Home Assistant Container
- Home Assistant Core

若畫面顯示目前的 Home Assistant 不支援重新導向，請返回 Home Assistant，並依上方「若按鈕無法自動開啟」步驟，從 HACS 手動加入本整合。

## 功能

- 網通設備流量來源管理：將 HA 既有速率實體或 SNMP 計數器送至 MAX’Is，以 Mbps 顯示。TP-Link、Deco、VIGI、Zyxel 的來源設定與型號限制見 [網通設備流量](docs/network-traffic.md)。
- 提供 REXLiTE AI 雲端服務與即時服務狀態。
- 服務暫時中斷時會自動恢復，減少人工處理。
- 安全啟用資料失效時停止無效重試，並由 Home Assistant 引導重新驗證。
- IPC 關機、重新開機或 DHCP 位址改變後，會沿用原服務身分重新連線並更新目前內網位址。
- 雲端服務可由使用者隨時開啟或關閉。
- 關閉雲端服務後，仍保留基本服務狀態。
- 支援繁體中文與英文介面。

## 關機、換網路與分階段開通

- 雲端連線使用固定的服務識別碼與安全啟用資料，不綁定安裝時的內網 IP。
- Home Assistant 每次啟動連線時都會重新偵測內網位址；運行期間若 DHCP 位址改變，也會再次上報。
- IPC 可以先由安裝流程完成雲端預配置並保持原服務身分，之後再由客戶 App 配對開通，不需要刪除或重裝整合。
- 若安裝情境要求先不開放雲端操作，可讓「雲端服務」保持關閉；基本健康連線會保留，畫面顯示「準備完成，等待開通」，使用者日後開啟時會以同一服務身分建立新的安全連線。

如需協助，請聯絡 REXLiTE AI 客戶服務。為保障帳號與服務安全，請勿在公開區域張貼啟用資料、服務位置或系統紀錄。
