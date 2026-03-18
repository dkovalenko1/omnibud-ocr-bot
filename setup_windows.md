# Налаштування CI/CD на офісному Windows ПК

## Передумови
- Python 3.11+ в PATH
- Git в PATH
- Права адміністратора

---

## 1. Клонування репозиторію

```powershell
cd C:\Apps
git clone https://github.com/dkovalenko1/omnibud-ocr-bot.git OmibudOCR
cd OmibudOCR
```

Скопіюйте вручну секретні файли (НЕ в репо):
```
C:\Apps\OmibudOCR\.env
C:\Apps\OmibudOCR\omni-*.json
```

Формат `.env`:
```
TELEGRAM_TOKEN=...
OPENAI_API_KEY=...
GOOGLE_SHEET_ID=...
WEBHOOK_SECRET=<той самий секрет що в GitHub webhook>
```

## 2. Встановлення залежностей

```powershell
cd C:\Apps\OmibudOCR
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

---

## 3. NSSM — Windows Services

Завантажте NSSM: https://nssm.cc/download
Розпакуйте, наприклад, в `C:\Tools\nssm\`.

### Сервіс бота (OmibudOCR)

```powershell
C:\Tools\nssm\win64\nssm.exe install OmibudOCR
```

У GUI вкажіть:
| Поле | Значення |
|------|----------|
| Path | `C:\Apps\OmibudOCR\.venv\Scripts\python.exe` |
| Startup directory | `C:\Apps\OmibudOCR` |
| Arguments | `bot.py` |

Вкладка **Details**: Display name = `OmibudOCR Bot`
Вкладка **Log on**: Local System
Вкладка **Exit actions**: Restart on crash, delay 5 sec

Або через командний рядок:
```powershell
nssm install OmibudOCR "C:\Apps\OmibudOCR\.venv\Scripts\python.exe" "bot.py"
nssm set OmibudOCR AppDirectory "C:\Apps\OmibudOCR"
nssm set OmibudOCR AppRestartDelay 5000
nssm set OmibudOCR Start SERVICE_AUTO_START
```

### Сервіс деплою (OmibudDeploy)

```powershell
nssm install OmibudDeploy "C:\Apps\OmibudOCR\.venv\Scripts\python.exe" "deploy_server.py"
nssm set OmibudDeploy AppDirectory "C:\Apps\OmibudOCR"
nssm set OmibudDeploy AppRestartDelay 5000
nssm set OmibudDeploy Start SERVICE_AUTO_START
```

### Запустити обидва сервіси

```powershell
nssm start OmibudOCR
nssm start OmibudDeploy
```

Перевірка: `services.msc` → OmibudOCR та OmibudDeploy мають статус **Running**.

---

## 4. Cloudflare Tunnel

### 4.1 Встановлення cloudflared

Завантажте `cloudflared-windows-amd64.exe` з:
https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/

Перейменуйте в `cloudflared.exe`, покладіть у `C:\Tools\cloudflared\`.

### 4.2 Авторизація та створення тунелю

```powershell
cloudflared login
cloudflared tunnel create omibud-deploy
```

Запишіть **Tunnel ID** (UUID) з виводу.

### 4.3 Конфігурація тунелю

Створіть файл `C:\Users\<User>\.cloudflared\config.yml`:

```yaml
tunnel: <TUNNEL_ID>
credentials-file: C:\Users\<User>\.cloudflared\<TUNNEL_ID>.json

ingress:
  - hostname: omibud-deploy.cfargotunnel.com
    service: http://localhost:9000
  - service: http_status:404
```

> Замініть `<TUNNEL_ID>` та `<User>` на свої значення.

### 4.4 DNS запис

```powershell
cloudflared tunnel route dns omibud-deploy omibud-deploy
```

### 4.5 Тунель як Windows Service

```powershell
cloudflared service install
```

Тепер тунель стартує автоматично разом з Windows.

Перевірте: відкрийте у браузері `https://omibud-deploy.cfargotunnel.com/health` — відповідь `OK`.

---

## 5. GitHub Webhook

1. GitHub → репозиторій → **Settings → Webhooks → Add webhook**
2. **Payload URL**: `https://omibud-deploy.cfargotunnel.com/deploy`
3. **Content type**: `application/json`
4. **Secret**: той самий рядок що в `.env` → `WEBHOOK_SECRET`
5. **Which events**: Just the push event
6. **Active**: ✓
7. **Add webhook**

---

## 6. Верифікація

### Перезавантаження ПК
```
Start → Services → OmibudOCR: Running ✓
                → OmibudDeploy: Running ✓
                → cloudflared: Running ✓
```

### Тестовий деплой
1. На dev машині:
   ```bash
   # Додайте коментар або порожній рядок у bot.py
   git add bot.py
   git commit -m "test: trigger deploy"
   git push origin main
   ```
2. Перевірте `C:\Apps\OmibudOCR\deploy.log`:
   ```
   2026-03-18 12:00:05 INFO Webhook received — running git pull
   2026-03-18 12:00:06 INFO git pull (exit 0): 1 file changed ...
   2026-03-18 12:00:06 INFO Changes detected — restarting service OmibudOCR
   2026-03-18 12:00:07 INFO nssm restart (exit 0): ...
   2026-03-18 12:00:07 INFO Deploy successful
   ```
3. Перевірте в Telegram що бот відповідає.

---

## Корисні команди

```powershell
# Статус сервісів
nssm status OmibudOCR
nssm status OmibudDeploy

# Перезапуск вручну
nssm restart OmibudOCR

# Логи deploy сервера
Get-Content C:\Apps\OmibudOCR\deploy.log -Tail 20

# Перевірка тунелю
cloudflared tunnel info omibud-deploy
```
