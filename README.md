# 🛠️ Discord 포트폴리오 봇

> 포인트·상점·AFK 자동이동·임시 보이스·팀편성·TTS·음악재생·랭킹·투표·OpenAI Q&A까지 한 번에 들어있는 디스코드 봇 템플릿.







> **주의**: 토큰/키는 반드시 **환경변수**로 관리하세요. (예: `.env`, CI/CD 시크릿)

---

## 목차

- [주요 기능](#주요-기능)
- [아키텍처 개요](#아키텍처-개요)
- [기술 스택](#기술-스택)
- [빠른 시작](#빠른-시작)
  - [사전 준비물](#사전-준비물)
  - [환경변수 설정](#환경변수-설정)
  - [설치 & 실행](#설치--실행)
- [기본 워크플로우](#기본-워크플로우)
- [명령어 레퍼런스](#명령어-레퍼런스)
- [포인트/AFK 로직](#포인트afk-로직)
- [음악 재생(Wavelink)](#음악-재생wavelink)
- [임시 채널 & 팀 생성](#임시-채널--팀-생성)
- [데이터베이스 구조](#데이터베이스-구조)
- [보안 체크리스트](#보안-체크리스트)
- [로드맵](#로드맵)
- [라이선스](#라이선스)

---

## 주요 기능

- 🎯 **포인트 시스템 v2 (SQLite)**: 보이스 체류 시간 기반 자동 포인트 적립, 관리자 지급/차감/설정, 상점/구매 로그.
- 💤 **AFK 자동이동**: 비활동 또는 장기 뮤트/이어폰 시 지정한 AFK 채널로 자동 이동.
- 🗣️ **TTS**: `edge-tts`로 한국어 합성, 캐시 후 FFmpeg 재생.
- 🎵 **음악**: `wavelink` 기반 유튜브/뮤직 검색 → 재생/스킵/정지.
- 🧩 **임시 보이스 채널**: 트리거 채널 입장 시 개인 보이스 생성/정리.
- 👫 **팀 편성**: 멘션한 유저를 점수 합 균형으로 2팀 자동 분배.
- 🧱 **팀 세트(1\~4팀) 자동 관리**: 카테고리 내 `팀 생성` 트리거 → 1\~4팀 생성, 모두 비면 자동 청소.
- 🏷️ **역할 버튼**: 멤버/지인, 포지션(탑/정글/미드/원딜/서폿) 단일 유지.
- 🧠 **Q&A**: OpenAI API를 활용한 간단 질의응답.
- 🗳️ **투표**: 이모지 리액션 기반 빠른 투표.
- 🏆 **랭킹**: 포인트/점수 리더보드, 게임 로그 출력.

---

## 아키텍처 개요

```
Discord Gateway ─┬─ discord.py(commands & app_commands)
                  │
                  ├─ Voice: Wavelink(Player) ── Lavalink(Server)
                  │
                  ├─ TTS: edge-tts → mp3 Cache → FFmpeg → Voice
                  │
                  ├─ DB: SQLite (points_v2.db, scores.db)
                  │      └─ users / guild_settings / shop / purchases / afk_watch / scores / match_logs
                  │
                  └─ OpenAI API (Q&A)
```

---

## 기술 스택

- **Python 3.10+**, `discord.py 2.x`
- **Wavelink 3.x** (Lavalink 4.x 필요)
- **SQLite** (내장 DB)
- **edge-tts**, **FFmpeg** (TTS 재생)
- **OpenAI Python SDK** (선택: Q&A)

---

## 빠른 시작

### 사전 준비물

- **Discord Bot 토큰** (봇 권한: `applications.commands`, `bot`, Privileged Intents: `SERVER MEMBERS`, `MESSAGE CONTENT`, `GUILD PRESENCES/VOICE STATES`)
- **FFmpeg** 설치
- **Lavalink 4.x** 서버 실행 (음악 기능 사용 시)

**Lavalink 예시(docker):**

```bash
docker run -d \
  -p 2333:2333 \
  -e SERVER_PORT=2333 \
  -e LAVALINK_PLUGINS_DIR=/plugins \
  --name lavalink ghcr.io/lavalink-devs/lavalink:4
```

### 환경변수 설정

`.env` 예시 (또는 OS 환경변수로 설정)

```env
DISCORD_TOKEN=your_discord_bot_token
OPENAI_API_KEY=sk-...
POINTS_DB_PATH=points_v2.db
```

> **중요**: 코드 내 하드코딩 금지. `.env`는 절대 공개 저장소에 커밋하지 마세요.

### 설치 & 실행

```bash
# 1) 클론 & 진입
git clone <YOUR_REPO_URL>
cd <YOUR_REPO_DIR>

# 2) 가상환경(권장)
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3) 의존성
pip install -U pip wheel
pip install discord.py wavelink edge-tts openai uvicorn

# 4) FFmpeg 설치 확인
ffmpeg -version

# 5) 환경변수 주입 후 실행
export DISCORD_TOKEN=...
export OPENAI_API_KEY=...
python main.py
```

> 첫 실행 시 Slash 명령이 자동 동기화됩니다(`bot.tree.sync()`).

---

## 기본 워크플로우

1. **로그 채널 지정(선택)**: `/set_log_channel #로그채널`
2. **AFK 채널 지정(선택)**: `/set_afk_channel <보이스채널>` 또는 `$set_afk <보이스채널>`
3. **임시 보이스 생성**: `칼바람 방 생성 / 솔랭 방 생성 / 방 생성` 채널 입장 → 개인 보이스 생성
4. **팀 세트 생성**: 카테고리명이 `칼바내전` 또는 `협곡내전`일 때 `팀 생성` 채널 입장 → 1\~4팀 자동 생성
5. **포인트 적립**: 보이스 체류 시 자동 적립(30분 단위 5p). 로그 채널을 지정했으면 적립 메시지 출력
6. **상점 관리**: `/shop_add`, `/shop`, `/buy`로 아이템 등록/구매

---

## 명령어 레퍼런스

### Prefix 명령어

| 명령                                              | 인자   | 설명                            | 권한  |
| ----------------------------------------------- | ---- | ----------------------------- | --- |
| `$?`                                            | -    | 도움말 출력                        | -   |
| `$role` / `$역할`                                 | -    | 역할 버튼 메시지 표시(멤버/지인/포지션 단일 유지) | -   |
| `$a` / `$질문 <질문>`                               | text | OpenAI Q&A 응답                 | -   |
| `$s <텍스트>`                                      | text | edge-tts 합성 후 음성 재생           | -   |
| `$sstop`                                        | -    | 현재 TTS 재생 중지                  | -   |
| `$rps @상대`                                      | 멘션   | 가위바위보 미니게임                    | -   |
| `$points` / `$포인트 [@유저]`                        | 멘션옵션 | 포인트 조회                        | -   |
| `$등록 [@유저]`                                     | 멘션옵션 | 랭킹용 초기 등록(점수 1000)            | -   |
| `$점수 [@유저]`                                     | 멘션옵션 | 랭킹 점수 조회                      | -   |
| `$랭킹`                                           | -    | 서버 랭킹 TOP10                   | -   |
| `$팀짜기 <@...>`                                   | 멘션N  | 멘션 대상 점수 균형 2팀 편성             | -   |
| `$로그`                                           | -    | 최근 경기 로그 5개 표시                | -   |
| `$투표 <질문>`                                      | text | 👍/❌/🤔 투표 메시지 생성             | -   |
| `$play <검색어>`                                   | text | 음악 검색 후 재생(유튜브 뮤직 우선)         | -   |
| `$resume` / `$skip` / `$stop`                   | -    | 음악 제어                         | -   |
| `$clear all` / `$clear <N>` / `$clear from @유저` | 가변   | 메시지 청소                        | 관리자 |
| `$setlog #채널`                                   | 채널   | 포인트 적립 로그 채널 설정               | 관리자 |
| `$set_afk <보이스채널>`                              | 채널   | AFK 채널 설정(프리픽스 버전)            | 관리자 |

### Slash 명령어

| 명령                 | 인자          | 설명                     | 권한  |
| ------------------ | ----------- | ---------------------- | --- |
| `/leaderboard`     | -           | 포인트 리더보드 TOP10         | -   |
| `/set_log_channel` | 채널?         | 포인트 로그 채널 세팅/해제        | 관리자 |
| `/set_afk_channel` | 보이스채널?      | AFK 채널 세팅/해제           | 관리자 |
| `/points_add`      | @유저, 양수     | 유저 포인트 추가              | 관리자 |
| `/points_remove`   | @유저, 양수     | 유저 포인트 차감(0 하한)        | 관리자 |
| `/points_set`      | @유저, 값      | 유저 포인트를 특정값으로 설정       | 관리자 |
| `/shop_add`        | 이름, 가격, 재고? | 상점 아이템 추가(재고 null=무제한) | 관리자 |
| `/shop`            | -           | 상점 목록 출력               | -   |
| `/buy`             | item\_id    | 상점 아이템 구매              | -   |
| `/give`            | @유저, 정수     | 포인트 증감(음수 허용)          | 관리자 |

> **트리거 채널명(임시 보이스)**: `칼바람 방 생성`, `솔랭 방 생성`, `방 생성`

> **팀 세트 트리거**: 카테고리명이 `칼바내전`/`협곡내전`일 때 `팀 생성` 보이스에 입장 → `1팀~4팀` 생성. 전부 비면 자동 삭제 & `팀 생성` 복구.

---

## 포인트/AFK 로직

- 상수
  - `POINTS_PER_BLOCK = 5`
  - `BLOCK_SECONDS = 30 * 60` (30분)
  - `AFK_SECONDS = 60 * 60` (60분 비활동 시 이동)
  - `MUTE_GRACE_SECONDS = 60 * 60` (장기 뮤트/이어폰 시 이동)
- 적립 방식
  - 보이스 입장 시 `last_join` 기록, 루프/이동 시 경과시간을 블록으로 환산하여 적립
  - 채널 이동/퇴장/주기 루프에서 합산 처리, 로그 채널 설정 시 적립 메시지 전송
- 비활동 판정
  - 텍스트 활동 시 `last_active` 갱신
  - 보이스 중 **뮤트/이어폰 아님**일 때도 주기적으로 `last_active` 갱신

---

## 음악 재생(Wavelink)

- `wavelink` 플레이어 사용 → **Lavalink 4.x** 서버 필요
- 검색 순서: `ytmsearch:` → 실패 시 `ytsearch:`
- 기본 명령: `$play`, `$resume`, `$skip`, `$stop`

> 지역/저작권 이슈로 검색 실패 가능. 별도 YouTube API Key는 사용하지 않으며, Lavalink 설정에 따릅니다.

---

## 임시 채널 & 팀 생성

- **임시 채널**: 트리거 보이스 입장 시 `방장: <닉네임>` 채널 생성, 빈 채널은 자동 삭제
- **팀 세트**: `1~4팀`을 하나의 그룹으로 관리, 모두 비면 세트 삭제 및 `팀 생성` 채널 복구
- **이름 정규화**: `칼 바 내 전`도 `칼바내전`으로 인식되도록 공백/특수문자 제거

---

## 데이터베이스 구조

### `points_v2.db`

- `users(guild_id, user_id, points, carry_sec, last_join)`
- `guild_settings(guild_id, afk_channel_id, log_channel_id)`
- `shop(id, guild_id, name, price, stock)`
- `purchases(id, guild_id, user_id, item_id, ts)`
- `afk_watch(guild_id, user_id, last_active)`

### `scores.db` (기존 랭킹 시스템)

- 예시 스키마(권장)

```sql
CREATE TABLE IF NOT EXISTS scores (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  username TEXT NOT NULL,
  score INTEGER NOT NULL DEFAULT 1000
);

CREATE TABLE IF NOT EXISTS match_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  winner_ids TEXT,
  loser_ids TEXT,
  note TEXT
);
```
---

### 스크린샷/데모

> 저장소에 스크린샷(`docs/`) 또는 GIF를 추가하면 이 섹션에 노출하세요.

