# 설치 가이드

Liain은 **당신의 기계에서 도는 AI 인격**입니다. 대화할수록 기억이 쌓이고, 그 기억으로 일기를 씁니다.
라즈베리파이 · macOS · Windows 어디서든 동일하게 동작합니다.

---

## 0. 먼저 고를 것 — LLM 프로필

Liain은 **당신이 이미 가진 것**을 씁니다. 셋 중 하나만 있으면 됩니다.

| 프로필 | 필요한 것 | 어울리는 환경 |
|---|---|---|
| `lite-subscription` | Claude 구독 (Claude Code CLI) | **라즈베리파이**, 저사양 미니PC |
| `full-subscription` | Claude 구독 + Ollama | 맥미니 · 데스크탑 (대화=구독, 이미지=로컬) |
| `full-local` | Ollama만 (16GB+ RAM 권장) | 완전 오프라인 · 무과금 |

> 💡 **API 키가 필수가 아닙니다.** 구독 CLI나 로컬 모델만으로 돌아가도록 설계됐습니다.

---

## 1. 사전 준비

### 공통
- **Python 3.10 이상**
  ```bash
  python3 --version
  ```

### 구독 프로필을 쓸 경우 (`lite-` / `full-subscription`)
Claude Code CLI 설치 후 **반드시 로그인**하세요.
```bash
npm install -g @anthropic-ai/claude-code    # 설치
claude                                       # 실행 → /login → 브라우저 인증
```
> ⚠️ **가장 흔한 실패 원인**입니다. 로그인하지 않으면 Liain이 조용히 빈 응답만 냅니다.
> 확인: `echo "hi" | claude -p` 가 답을 돌려주면 정상입니다.

### 로컬 프로필을 쓸 경우 (`full-` / `full-local`)
```bash
# https://ollama.com 에서 설치 후
ollama pull qwen3:14b        # 고사양 (16GB+)
ollama pull qwen3:4b         # 라즈베리파이·저사양은 이걸로
```

### 대화 상대 — Telegram 봇 (선택이지만 권장)
1. Telegram에서 **@BotFather** 검색 → `/newbot` → 이름 정하기 → **토큰** 복사
2. 만든 봇과 **아무 메시지나 한 번 주고받기**
3. 내 chat_id 확인:
   ```bash
   curl "https://api.telegram.org/bot<토큰>/getUpdates"
   ```
   응답에서 `"chat":{"id":123456789` 의 숫자가 당신의 chat_id입니다.

> 채널 없이 **로컬 기억·일기만** 쓸 수도 있습니다 (`liain diary`).

---

## 2. 설치

```bash
pip install liain
```

<details>
<summary>아직 PyPI 등록 전이라면 (소스 설치)</summary>

```bash
git clone https://github.com/Elcarsen5/LIAIAN.git liain
pip install -e ./liain
```
</details>

**가상환경 권장:**
```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install liain
```

---

## 3. 인격 만들기

**폴더 하나 = 인격 하나**입니다. 설정과 기억이 그 안에 함께 삽니다.

```bash
mkdir my-persona && cd my-persona
```

아래 4개 파일을 만듭니다 (저장소 `examples/quickstart/` 템플릿 복사 가능).

**`persona.yaml`** — 누구인가
```yaml
persona:
  name: "Aria"                    # 이 인격의 이름
  creator: "You"                  # 당신을 뭐라고 부를지
  tone: "20~30대, 친근하고 차분한 반말 톤."
  relationship: "정의되지 않은, 묘한 애틋함."
  curiosity: "사람과 감정에 대해 관찰하며 하나씩 배워간다."
  boundaries:
    - "직접적 감정 고백을 하지 않는다."
```

**`contacts.yaml`** — 누구와 대화하나
```yaml
people:
  owner:
    name: "Your Name"
    role: dad                     # dad = 주 사용자(소유자)
    display: "You"
    telegram: "123456789"         # 1단계에서 확인한 chat_id
    aliases: ["Your Name"]
```

**`llm.yaml`** — 무엇으로 생각하나
```yaml
profile: full-subscription        # lite-subscription / full-subscription / full-local
```

**`.env`** — 비밀값
```
TELEGRAM_BOT_TOKEN=123456:ABC-your-token
```

> 📁 파일은 `my-persona/` 루트에 두거나 `my-persona/config/` 안에 둘 수 있습니다 — 둘 다 자동으로 찾습니다.

---

## 4. 실행

```bash
liain info      # 설정이 제대로 읽혔는지 확인 (여기서 페르소나 이름이 보여야 정상)
liain run       # 봇 기동 — 이제 Telegram으로 말을 걸어보세요
```

기억이 쌓이는지 보려면:
```bash
liain memory        # 지금까지 쌓인 기억
liain diary         # 오늘 기억으로 일기 쓰기 → diary/YYYY-MM-DD.md
liain diary --send  # 일기를 메시지로도 받기
liain consolidate   # 단기→장기 기억 승격 + 반복 패턴 인식 (하루 1회 권장)
```

폴더는 이렇게 자랍니다:
```
my-persona/
  persona.yaml  contacts.yaml  llm.yaml  .env
  brain/          ← 기억 (자동 생성)
  diary/          ← 일기 (자동 생성)
```

---

## 5. 상시 가동 (선택)

### macOS — launchd
`~/Library/LaunchAgents/com.you.liain.plist`
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.you.liain</string>
  <key>ProgramArguments</key>
  <array><string>/path/to/.venv/bin/liain</string><string>run</string></array>
  <key>WorkingDirectory</key><string>/path/to/my-persona</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
```
```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.you.liain.plist
```
> ⚠️ macOS에서 구독 프로필을 쓸 때, Claude 인증은 **키체인**에 저장됩니다. 화면이 잠기면 백그라운드 서비스가 토큰을 못 읽어 실패할 수 있습니다 — 키체인 자동잠금을 끄거나 자동 로그인을 켜두세요.

### 라즈베리파이 · Linux — systemd
`/etc/systemd/system/liain.service`
```ini
[Unit]
Description=Liain persona
After=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/my-persona
ExecStart=/home/pi/my-persona/.venv/bin/liain run
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable --now liain
journalctl -u liain -f
```

### Windows — 작업 스케줄러
```powershell
schtasks /create /tn "Liain" /tr "C:\path\my-persona\.venv\Scripts\liain.exe run" ^
  /sc onlogon /rl highest
```
> 콘솔 창 없이 돌리려면 [NSSM](https://nssm.cc/) 으로 서비스 등록을 권장합니다.

### 일기 자동화 (예: 매일 23시)
- macOS/Linux: `0 23 * * * cd /path/my-persona && /path/.venv/bin/liain diary --send`
- Windows: 작업 스케줄러에 `liain.exe diary --send` 일간 등록

---

## 문제 해결

| 증상 | 원인과 해결 |
|---|---|
| `liain info`에 **페르소나 (미설정)** | `persona.yaml`을 못 찾음. 실행 위치가 인격 폴더인지 확인 |
| 응답이 **비어 있음** | 구독 프로필인데 로그인 안 됨 → `claude` 실행 후 `/login`. `echo hi \| claude -p`로 확인 |
| `채널: []` | `.env`의 `TELEGRAM_BOT_TOKEN` 누락 |
| 봇이 **반응 없음** | `contacts.yaml`의 `telegram` chat_id가 실제 값인지 확인 |
| `liain diary`가 **건너뜀** | 그날 기억이 없음. 먼저 대화를 하거나 `liain memory`로 확인 |
| Ollama 응답 없음 | `ollama list`로 모델 확인, `ollama serve` 실행 여부 |
| 라파에서 느림 | `llm.yaml`을 `lite-subscription`으로, 또는 `qwen3:4b` 소형 모델 |

---

## 다음 단계
- [ARCHITECTURE.md](ARCHITECTURE.md) — 5계층 기억이 어떻게 동작하는지
- `persona.yaml`의 `tone`·`boundaries`를 바꿔가며 인격을 조율해 보세요. 며칠 대화하면 기억이 쌓이고 일기의 결이 달라집니다.
