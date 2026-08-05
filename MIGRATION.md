# 서버용 맥으로 이전 가이드

경매 크롤러를 다른 맥(서버용)으로 통째로 옮기는 절차. 같은 와이파이에서 `rsync`로 전송한다.

- **원본 맥**: 현재 맥 (IP `192.168.219.54`, 사용자 `bym`)
- **대상 맥**: 새 서버용 맥 ("BYM의 MacBook Air")
- **옮기는 것**: 코드(git) · DB 1.9G · 사진·문서 11G · API 키(.env) · 대화·메모리 · launchd 설정

> ⚠️ 전송 중 양쪽 맥에서 데몬이 동시에 돌면 중복 수집 + API 한도 이중 소모가 된다.
> 이전이 끝나면 **원본 맥의 데몬은 반드시 완전히 끈다**(맨 아래 6단계).

---

## 0. 대상 맥에서 사용자명 먼저 확인

터미널에서:
```bash
whoami
```
- `bym`이 나오면 → 모든 경로가 그대로라 가장 간단(아래 그대로 따라감)
- 다른 이름이면 → 이 문서의 `bym`을 전부 그 이름으로 바꿔 읽고, **plist 경로 치환**(5단계 주석) 필요

---

## 1. 원본 맥: 데몬 정지 + 코드 푸시 + 원격 로그인 켜기

**(가) 데몬 정지** — DB가 흔들리지 않게 수집을 멈춘다:
```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.court-auction.collect.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.court-auction.collect-details.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.court-auction.server.plist
```

**(나) 코드 커밋 + 푸시** — 새 맥은 이걸 clone으로 받는다:
```bash
cd "/Users/bym/Documents/경매물건 크롤링"
git add -A && git commit -m "건축물대장·실거래가 API 통합" && git push
```

**(다) 원격 로그인 켜기** — rsync가 SSH로 붙는다:
`시스템 설정 > 일반 > 공유 > 원격 로그인` **켜기** (bym 계정 허용 확인)

---

## 2. 대상 맥: 기본 도구 설치

```bash
# Homebrew (없으면)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# git, python 3.14
brew install git python@3.14

# 프로젝트 clone
mkdir -p ~/Documents && cd ~/Documents
git clone git@github.com:BYM117/court-auction-crawler.git "경매물건 크롤링"
# ↑ SSH 키가 새 맥에 없으면 https 주소로:
#   git clone https://github.com/BYM117/court-auction-crawler.git "경매물건 크롤링"
```

---

## 3. 대상 맥: 파이썬 환경 + 브라우저

```bash
cd ~/Documents/"경매물건 크롤링"
python3.14 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install pytest        # 테스트용(선택)
.venv/bin/python -m playwright install chromium
```

동작 확인:
```bash
PYTHONPATH=src .venv/bin/python -m pytest -q     # 85 passed 나오면 코드 정상
```

---

## 4. 대상 맥: 데이터·키·대화 당겨오기 (rsync)

원본 맥이 켜져 있고 원격 로그인이 켜진 상태에서, **대상 맥**에서 실행한다.

**(가) API 키(.env)** — git에 없으므로 따로:
```bash
rsync -avz bym@192.168.219.54:"/Users/bym/Documents/경매물건 크롤링/.env" \
  ~/Documents/"경매물건 크롤링"/.env
```

**(나) 데이터(DB + 사진·문서 13G)** — 백업·WAL 제외:
```bash
rsync -avz --progress \
  --exclude='*.backup*' --exclude='*-wal' --exclude='*-shm' --exclude='*.pid' \
  bym@192.168.219.54:"/Users/bym/Documents/경매물건 크롤링/data/" \
  ~/Documents/"경매물건 크롤링"/data/
```
> 13G라 시간이 걸린다. 중단되면 같은 명령을 다시 실행하면 이어받는다(rsync 특성).

**(다) 이 대화 + 메모리** — Claude Code 세션과 자동 메모리:
```bash
rsync -avz \
  bym@192.168.219.54:"/Users/bym/.claude/projects/-Users-bym-Documents---------/" \
  ~/.claude/projects/-Users-bym-Documents---------/
```
> 대상 맥 사용자명이 `bym`이 아니면 이 폴더명(`-Users-bym-...`)도 새 경로 인코딩으로 바뀌어야 하므로, 그럴 땐 먼저 대상 맥에서 `claude`를 이 프로젝트 폴더에서 한 번 실행해 폴더를 생성시킨 뒤 그 이름에 맞춰 복사한다. 이후 `claude --resume`으로 이 대화를 이어갈 수 있다.

---

## 5. 대상 맥: launchd 데몬 등록

**(가) plist 3개를 LaunchAgents로 복사:**
```bash
cp ~/Documents/"경매물건 크롤링"/launchd/com.court-auction.*.plist ~/Library/LaunchAgents/
```

> **사용자명이 bym이 아니면** 복사 전에 경로를 치환한다:
> ```bash
> cd ~/Documents/"경매물건 크롤링"/launchd
> for f in com.court-auction.*.plist; do
>   sed "s#/Users/bym/#/Users/$(whoami)/#g" "$f" > ~/Library/LaunchAgents/"$f"
> done
> ```

**(나) /bin/zsh에 전체 디스크 접근 부여** (TCC — 이게 없으면 데몬이 문서 폴더 접근 실패):
`시스템 설정 > 개인정보 보호 및 보안 > 전체 디스크 접근` → `+` → `⌘⇧G`로 `/bin/zsh` 추가 후 **켜기**

**(다) 데몬 로드:**
```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.court-auction.server.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.court-auction.collect.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.court-auction.collect-details.plist
```

---

## 6. 검증 + 원본 맥 정리

**대상 맥에서 검증:**
```bash
launchctl list | grep court-auction          # 3개 떠 있으면 OK
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/   # 200이면 서버 정상
```
브라우저로 `http://127.0.0.1:8000` 열어 물건 목록·상세(건축물정보·실거래가)까지 보이면 이전 완료.

**원본 맥 정리** — 이제 대상 맥이 서버이므로 원본 데몬은 끈 채로 둔다(1단계 (가)에서 이미 정지). 완전히 손 떼려면 plist도 제거:
```bash
rm ~/Library/LaunchAgents/com.court-auction.*.plist
```

---

## 참고

- **매일 아침 상태확인**: 맥 잠자기로 야간 사이클이 누락되므로, 대상 맥에서도 아침마다 상태확인 시 백필을 재개한다(`caffeinate` 동반). 서버 맥이 늘 깨어 있다면(잠자기 끔) 이 문제는 사라진다 — 서버용이라면 `시스템 설정 > 배터리/전원 > 디스플레이 꺼져도 자동 잠자기 방지`를 켜는 걸 권장.
- **API 키 재발급**: `PUBLIC_DATA_SERVICE_KEY`가 과거 노출된 적 있어, 이전을 계기로 공공데이터포털에서 재발급 후 양쪽 `.env` 갱신을 검토.
- **공유 키 주의**: VWorld 키는 꽁지맵 프로젝트와 공유. 실거래가/건축물대장 한도는 API 오퍼레이션별 일일 제한(초과 시 HTTP 429).
