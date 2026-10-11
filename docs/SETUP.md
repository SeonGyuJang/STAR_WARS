# STAR WARS 설치·설정 가이드

코드는 그대로 두고, 운영하는 사람이 직접 준비해야 하는 것만 순서대로 정리했습니다.
처음 설치한다면 1번부터 5번까지 진행한 뒤, Heroku로 배포하려면 6번을, 직접 서버를 운영하려면 7번을 따르면 됩니다.

## 한눈에 보기

| 항목 | 꼭 필요? | 어디서 받나 | 어디에 넣나 |
|---|---|---|---|
| Anthropic API 키 | **필수** | Claude Console | `.env`의 `ANTHROPIC_API_KEY` |
| 결제 수단 또는 크레딧 | **필수** | Claude Console의 결제 메뉴 | 콘솔 |
| `SECRET_KEY` | **필수** | 직접 생성 (아래 명령) | `.env` |
| `ADMIN_PASSWORD` | 공개 서버라면 필수 | 직접 정함 | `.env` |
| `OPENALEX_MAILTO` | 권장 | 본인 이메일 | `.env` |
| `SEMANTIC_SCHOLAR_API_KEY` | 선택 | Semantic Scholar 신청 폼 | `.env` |
| 인스타 계정명 | 권장 | 본인 계정 | `.env`의 `CARD_HANDLE` |
| Chromium (카드뉴스 렌더링) | **필수** | `playwright install` | 서버 |
| 외부 접속 허용 도메인 | 방화벽이 있다면 필수 | 아래 4번 목록 | 서버·클라우드 방화벽 |
| Heroku Postgres, Scheduler 애드온 | Heroku 배포 시 필수 | Heroku | 6번 참고 (`DATABASE_URL`은 자동) |
| Chrome 빌드팩 | Heroku 배포 시 필수 | Heroku | 6번 참고 |

> Google Drive 연동은 없습니다. PDF는 서버의 임시 폴더에 내려받아 Claude가 읽고 그림을 뽑은 뒤 바로 지웁니다.
> 사용자에게는 arXiv 같은 원문 PDF 링크를 보여 줍니다. 서버에 남는 것은 DB, 추출한 그림 PNG, 카드뉴스 PNG뿐입니다.

---

## 1. 서버 준비

Python 3.10 이상이 필요합니다. Linux(Ubuntu 22.04 이상)를 기준으로 설명합니다. macOS나 Windows(WSL)에서도 같은 명령으로 동작합니다.

```bash
git clone https://github.com/SeonGyuJang/STAR_WARS.git
cd STAR_WARS
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 카드뉴스를 PNG로 찍는 헤드리스 브라우저와, 실행에 필요한 리눅스 라이브러리
python -m playwright install --with-deps chromium
```

- `--with-deps`는 `sudo` 권한으로 시스템 패키지를 설치합니다. 권한이 없다면 서버 관리자에게 `python -m playwright install-deps chromium` 실행을 요청하세요.
- 한글 폰트(Pretendard)는 저장소에 포함돼 있어서 서버에 따로 설치하지 않아도 됩니다.
- 이미 Chromium이 설치된 서버라면 `.env`의 `CHROMIUM_EXECUTABLE=/usr/bin/chromium`처럼 경로를 지정해도 됩니다.

설정 파일을 만듭니다.

```bash
cp .env.example .env
```

`.env`는 비밀 정보가 들어가는 파일이라 `.gitignore`에 들어 있습니다. 절대 커밋하지 마세요.

---

## 2. Anthropic API 키 (필수)

에이전트가 논문을 읽고 평가하는 부분이 Claude API를 씁니다.

### 2-1. 키 발급

1. [Claude Console](https://platform.claude.com)에 가입하거나 로그인합니다.
2. 설정(Settings)의 **API Keys** 메뉴에서 **Create Key**를 누릅니다. 이름은 `star-wars-server`처럼 용도를 알 수 있게 짓습니다.
3. 만들어진 키(`sk-ant-...`)는 이때 한 번만 보이니 바로 복사합니다.
4. `.env`에 넣습니다.

```bash
ANTHROPIC_API_KEY=sk-ant-여기에-붙여넣기
```

### 2-2. 결제와 사용 한도

- API는 사용한 만큼 비용이 나갑니다. 콘솔의 결제(Billing) 메뉴에서 결제 수단을 등록하거나 크레딧을 충전해야 호출이 됩니다. 잔액이 없으면 요청이 거절됩니다.
- **월 사용 한도(spend limit)를 꼭 걸어 두세요.** 콘솔의 한도(Limits) 메뉴에서 설정할 수 있습니다. 실행 주기나 논문 수 설정을 잘못 넣어도 한도 이상은 나가지 않습니다.

### 2-3. 비용 감 잡기: 단계마다 다른 모델

논문 한 편을 세 단계로 나눠, 각 단계에 꼭 필요한 만큼의 모델만 씁니다.

| 단계 | 하는 일 | 기본 모델 | 가격 (입력/출력, 100만 토큰당) | 논문 1편 비용 (20쪽 기준, 대략) |
|---|---|---|---|---|
| 관련도 판단 | 제목·초록으로 0~10점 | Haiku 5.5 | $0.10 / $0.50 | 실행 1회 전체 약 $0.01 |
| 정독 | PDF 전문을 읽고 근거 노트 작성 (입력이 가장 큼) | Haiku 5.5 | $0.10 / $0.50 | 약 $0.01 |
| 평가 | 노트만 보고 방법론·참신성 채점 (가장 중요) | Opus 5.5 | $4 / $20 | 약 $0.09~0.15 |
| 작성 | 한국어 리뷰와 카드뉴스 문구 | Sonnet 5.5 | $2 / $10 | 약 $0.05~0.07 |

비싼 Opus는 PDF 전체(입력 4만~6만 토큰)를 보지 않습니다. 정독 모델이 정리한 수천 토큰짜리 노트만 읽고 점수를 정합니다.
노트에는 실험 설정, 베이스라인, 수치, 통계 처리, 재현성, 의심 지점처럼 채점에 필요한 근거가 모두 들어갑니다.

| 설정 | 논문 1편 | 실행 1회 | 하루 |
|---|---|---|---|
| 예전 (전부 Opus, 6시간마다 5편) | 약 $0.4 | 약 $2.3 | 약 $9 |
| **지금 기본값 (12시간마다 3편, 관련도 7 이상)** | **약 $0.15~0.22** | **약 $0.5~0.7** | **약 $1~1.4** |
| 절약 설정 (아래 두 줄 추가) | 약 $0.08~0.12 | 약 $0.3 | 약 $0.6 |

더 줄이고 싶다면 `.env`나 Heroku Config Vars에 아래 두 줄을 넣으세요.
```bash
JUDGE_EFFORT=medium            # 평가 모델이 생각하는 양을 줄임 (점수 품질은 약간 내려갈 수 있음)
WRITER_MODEL=claude-haiku-5-5  # 한국어 문장이 약간 단조로워질 수 있음
```

- 실제로 쓴 비용은 대시보드에서 확인할 수 있습니다. **설정 → 모델과 비용**에 최근 30일 합계와 논문당 평균이, 실행 기록에 실행별 비용이, 논문 상세 페이지에 리뷰 비용이 나옵니다.
- Haiku는 한 번에 보내는 입력이 10만 토큰을 넘으면 요금이 5배($0.50 / $2.50)가 됩니다. 100쪽이 넘는 아주 긴 논문에서만 해당되고, 그래도 Opus보다 훨씬 쌉니다.

### 2-4. 키가 잘 동작하는지 확인

토큰을 쓰지 않는 확인 명령입니다.

```bash
python -c "import anthropic; c = anthropic.Anthropic(); [print(c.models.retrieve(m).display_name) for m in ('claude-haiku-5-5', 'claude-opus-5-5', 'claude-sonnet-5-5')]"
```

모델 이름 세 개가 출력되면 성공입니다. `.env`를 쓰고 있다면 `set -a; source .env; set +a`를 먼저 실행하세요. 앱 자체는 `.env`를 자동으로 읽습니다.

### 2-5. AWS Bedrock·Google Vertex 등으로 Claude를 쓰는 경우

이 프로젝트는 Anthropic API를 직접 쓰도록 만들어져 있습니다. 다른 플랫폼을 쓰려면 `paperagent/agent.py`의 클라이언트 생성 부분을 해당 플랫폼용으로 바꾸고, `.env`에 `CLAUDE_FALLBACKS=0`을 넣으세요. 거절 시 자동 대체 모델 기능은 Anthropic API의 Opus·Sonnet에서만 지원됩니다.

---

## 3. 나머지 `.env` 항목

### `SECRET_KEY` (필수)
로그인 세션과 CSRF 보호에 쓰는 비밀값입니다. 아래 명령으로 만든 값을 넣으세요.

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### `ADMIN_PASSWORD` (공개 서버라면 필수)
- **비워 두면:** 접속한 누구나 키워드·저널을 바꾸고 실행 버튼을 누를 수 있습니다.
- **설정하면:** 대시보드 보기는 누구나 할 수 있고, 변경과 실행은 로그인한 관리자만 할 수 있습니다. 긴 비밀번호를 쓰세요.

### `OPENALEX_MAILTO` (권장)
본인 이메일을 넣습니다. OpenAlex는 키가 필요 없지만, 이메일을 보내는 요청을 더 안정적인 "polite pool"로 처리해 줍니다.

### `SEMANTIC_SCHOLAR_API_KEY` (선택)
"리뷰한 논문의 인용 변화" 패널에 씁니다. 키가 없어도 동작하지만, 공용 한도를 같이 쓰기 때문에 가끔 요청이 거절될 수 있습니다.
[Semantic Scholar API 페이지](https://www.semanticscholar.org/product/api)의 키 신청 폼을 작성하면 보통 며칠 안에 메일로 받습니다.

### 카드뉴스 브랜드
```bash
CARD_HANDLE=@실제_인스타_계정
CARD_BRAND=STAR WARS
```

### 실행 기본값
`FETCH_INTERVAL_HOURS`, `MAX_FULL_READS`, `MIN_RELEVANCE` 등은 처음 한 번만 쓰이는 기본값입니다. 이후에는 대시보드 **설정** 페이지에서 바꾼 값이 우선합니다.

---

## 4. 외부 접속 허용 목록

회사망이나 클라우드 방화벽처럼 나가는 접속을 막는 환경이라면 아래 도메인의 HTTPS(443)를 열어 주세요.

| 도메인 | 용도 |
|---|---|
| `api.anthropic.com` | Claude API |
| `export.arxiv.org` | arXiv 새 논문 목록 |
| `arxiv.org` | 논문 PDF 다운로드 |
| `api.openalex.org` | 저널 논문, 인용 수, 저널 검색 |
| `huggingface.co` | 오늘 화제인 논문 |
| `api.semanticscholar.org` | 리뷰한 논문의 인용 추적 |
| 각 저널의 오픈액세스 PDF 호스트 | 저널 논문 PDF (출판사마다 다름) |

저널 PDF 호스트가 막혀 있으면 해당 논문은 초록만 읽고 "초록만 읽음"으로 표시됩니다.

---

## 5. 첫 실행과 확인

```bash
# 1) 논문 한 편으로 전체 흐름 확인: 다운로드 → 리뷰 → 카드뉴스 (1~3분)
python -m paperagent review 1706.03762

# 2) 대시보드 실행 후 브라우저로 http://서버주소:8000 접속
python -m paperagent serve --port 8000
```

확인할 것:
- 첫 화면 목록에 방금 리뷰한 논문이 보이는지
- 논문 상세 페이지에 평점, 요약, 카드뉴스 6장이 나오는지
- "원문 PDF 받기" 버튼이 arXiv PDF로 연결되는지

서버를 켜면 30초 뒤 전체 파이프라인이 한 번 돌고, 이후 설정한 간격마다 반복합니다. 진행 상황은 **설정 → 실행 기록**에서 볼 수 있습니다.

---

## 6. Heroku에 배포하기 (권장)

저장소에 Heroku용 파일이 들어 있습니다.

| 파일 | 역할 |
|---|---|
| `Procfile` | 웹 서버(gunicorn)와 배포 때 DB 테이블을 만드는 release 명령 |
| `app.json` | 필요한 애드온(Postgres, Scheduler), 빌드팩, 환경 변수 목록 |
| `.python-version` | Python 3.12 |
| `.slugignore` | 테스트·문서 이미지를 배포 용량에서 제외 |

### Heroku에서 달라지는 점 (코드에 이미 반영됨)

| Heroku의 특성 | 대응 |
|---|---|
| 디스크가 재시작할 때마다(하루 1회 이상) 지워짐 | 데이터는 **Heroku Postgres**에 저장합니다. 카드뉴스와 그림 이미지도 DB에 들어갑니다 (`DATABASE_URL`이 있으면 자동). |
| 웹 dyno 안의 타이머는 재시작·잠자기 때문에 믿을 수 없음 | **Heroku Scheduler**가 매시간 `python -m paperagent run --if-due`를 실행합니다. 설정한 간격(12시간)이 지났을 때만 실제로 돌기 때문에 비용은 그대로입니다. |
| 여러 dyno에서 같은 작업이 겹칠 수 있음 | 실행 상태를 DB에 기록해서 다른 dyno가 돌고 있으면 건너뜁니다. |
| Chromium이 기본으로 없음 | **chrome-for-testing 빌드팩**이 Chrome을 설치하고, 카드 렌더러가 자동으로 찾아 씁니다. |
| PDF 저장 공간 | PDF는 `/tmp`에서 읽고 바로 지우므로 문제없습니다. |

### 6-1. 처음 배포

[Heroku CLI](https://devcenter.heroku.com/articles/heroku-cli)를 설치하고 로그인한 뒤, 저장소 폴더에서 실행합니다.

```bash
heroku login
heroku create my-starwars              # 앱 이름은 원하는 대로

# 빌드팩: Chrome(카드뉴스 렌더링) 먼저, Python 나중
heroku buildpacks:add -i 1 https://github.com/heroku/heroku-buildpack-chrome-for-testing
heroku buildpacks:add -i 2 heroku/python

# 애드온: DB와 스케줄러
heroku addons:create heroku-postgresql:essential-0
heroku addons:create scheduler:standard

# 환경 변수 (Config Vars)
heroku config:set \
  ANTHROPIC_API_KEY=sk-ant-... \
  SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))") \
  ADMIN_PASSWORD='긴-비밀번호' \
  OPENALEX_MAILTO=you@example.com \
  CARD_HANDLE=@실제_인스타_계정

# 배포 (기본 브랜치 이름이 main이 아니면 바꿔서)
git push heroku main
```

`DATABASE_URL`은 Postgres 애드온이 자동으로 넣어 줍니다. 배포가 끝나면 release 단계에서 DB 테이블이 만들어집니다.

> 브라우저로 하고 싶다면 Heroku 대시보드에서 앱을 만들고 **Settings → Buildpacks**와 **Resources → Add-ons**, **Settings → Config Vars**에서 위와 같은 항목을 추가하면 됩니다. GitHub 저장소를 연결하면 **Deploy** 탭에서 버튼으로 배포할 수 있습니다.

### 6-2. 스케줄러 등록 (한 번만)

```bash
heroku addons:open scheduler
```

열린 화면에서 **Create job**을 누르고 아래처럼 입력합니다.

| 항목 | 값 |
|---|---|
| Schedule | **Every hour at :00** |
| Run Command | `python -m paperagent run --if-due` |
| Dyno size | **Standard-2X** 권장 (Chrome과 PDF 처리에 메모리 1GB). Basic(512MB)도 동작하지만 메모리 경고(R14)가 날 수 있습니다. |

매시간 짧게 깨어나서 "지난 실행 후 12시간이 지났는지" 확인하고, 지났을 때만 수집과 리뷰를 합니다. 간격은 대시보드 설정 페이지에서 바꾸면 바로 반영됩니다.

### 6-3. 확인

```bash
heroku logs --tail                                  # 로그 보기
heroku run python -m paperagent review 1706.03762   # 논문 한 편으로 전체 흐름 확인
heroku open                                         # 대시보드 열기
```

수동 실행을 메모리가 넉넉한 별도 dyno에서 돌리려면 `heroku run:detached --size=standard-2x python -m paperagent run`을 쓰세요.
대시보드의 "지금 실행", "바로 리뷰", "카드 다시 만들기" 버튼은 웹 dyno 안에서 Chrome을 띄웁니다. 웹 dyno가 Basic(512MB)이면 메모리가 빠듯할 수 있습니다.

### 6-4. Heroku 비용 (참고, [Heroku 요금](https://www.heroku.com/pricing) 기준으로 확인하세요)

| 항목 | 대략 |
|---|---|
| 웹 dyno (Basic) | 월 약 $7 |
| Heroku Postgres (Essential-0, 1GB) | 월 약 $5 |
| Scheduler 실행 dyno | 사용한 시간만큼 (하루 몇 분, 월 $1 미만) |

카드뉴스와 그림은 논문 한 편에 2~4MB 정도 DB를 씁니다. 하루 6편이면 1GB를 두 달쯤 쓰므로, 오래 운영할 계획이라면 Essential-1 이상으로 올리세요.
오래된 이미지만 지우고 싶을 때는 `heroku pg:psql`에서 `DELETE FROM blobs WHERE updated_at < '2026-01-01';`처럼 지울 수 있습니다. 리뷰 텍스트는 그대로 남습니다.

### 6-5. 백업

```bash
heroku pg:backups:schedule DATABASE_URL --at '04:00 Asia/Seoul'   # 매일 자동 백업
heroku pg:backups:capture                                         # 지금 백업
heroku pg:backups:download                                        # 내려받기
```

## 7. (대안) 내 서버에서 상시 운영

### 7-1. systemd 서비스

`/etc/systemd/system/starwars.service`:

```ini
[Unit]
Description=STAR WARS paper agent
After=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/STAR_WARS
ExecStart=/home/ubuntu/STAR_WARS/.venv/bin/gunicorn -w 1 --threads 8 --timeout 120 -b 127.0.0.1:8000 wsgi:app
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now starwars
journalctl -u starwars -f      # 로그 보기
```

- **워커는 반드시 1개(`-w 1`)로 두세요.** 워커가 여러 개면 스케줄러도 여러 개 떠서 같은 논문을 중복으로 읽고 비용이 배로 나갑니다. 동시 접속은 `--threads`로 늘리면 됩니다.
- `WorkingDirectory`가 저장소 폴더여야 `.env`를 읽습니다.

### 7-2. 외부에서 접속 가능하게

둘 중 하나를 고르세요.

**A. 도메인 + Nginx + HTTPS**
```nginx
server {
    server_name papers.example.com;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```
```bash
sudo certbot --nginx -d papers.example.com   # 무료 HTTPS 인증서
```

**B. 도메인이나 포트 개방 없이 빠르게 공유: Cloudflare Tunnel**
```bash
cloudflared tunnel --url http://localhost:8000
```
출력되는 `https://...trycloudflare.com` 주소를 공유하면 됩니다. 계속 쓸 거라면 Cloudflare 계정에서 이름 있는 터널을 만드세요.

---

## 8. 데이터와 백업 (내 서버에서 운영할 때)

| 경로 | 내용 | 크기 |
|---|---|---|
| `data/paperagent.db` | 논문 목록, 리뷰, 설정, 실행 기록 | 작음 (수 MB) |
| `data/figures/<id>/` | 논문에서 추출한 그림 PNG (카드 재생성용) | 논문당 약 1~3MB |
| `data/cards/<id>/` | 카드뉴스 PNG 6장, 캡션 | 논문당 약 2~4MB |

- PDF는 저장하지 않으므로 디스크가 빠르게 늘지 않습니다.
- 백업은 `data/` 폴더만 복사하면 됩니다. 다른 위치에 두려면 `DATA_DIR`을 지정하세요.
- 오래된 그림과 카드를 지워도 리뷰 텍스트는 DB에 남습니다. 그 논문의 카드를 다시 만들려면 "다시 읽기"를 누르세요.

---

## 9. 인스타그램 업로드

현재는 **수동 업로드**입니다.
1. 논문 상세 페이지에서 **전체 받기**를 눌러 카드 6장과 `caption.txt`가 든 ZIP을 받습니다.
2. **캡션 복사**를 누르고 인스타에 붙여 넣습니다.

자동 게시는 Instagram Graph API로 만들 수 있습니다. 다만 비즈니스 또는 크리에이터 계정, Facebook 페이지 연결, Meta 앱 등록과 권한 심사가 필요해서 이번 범위에는 넣지 않았습니다. 이미지는 공개 URL로 접근할 수 있어야 하는데, 대시보드의 `/files/card/<id>/<n>` 주소를 그대로 쓸 수 있습니다.

---

## 10. 문제 해결

| 증상 | 원인과 해결 |
|---|---|
| 실행 기록에 `401` / `authentication_error` | API 키가 틀렸거나 비어 있습니다. `.env`의 `ANTHROPIC_API_KEY`를 확인하고 서비스를 재시작하세요. |
| `400`에 credit / billing 관련 메시지 | 크레딧이 없거나 결제 수단이 없습니다. 콘솔 결제 메뉴에서 확인하세요. |
| `429` / `rate_limit_error` | 짧은 시간에 요청이 너무 많습니다. 자동으로 재시도합니다. 자주 나오면 "한 번에 정독할 논문" 수를 줄이세요. |
| `card rendering failed: Could not launch Chromium` | `python -m playwright install --with-deps chromium`을 다시 실행하거나 `CHROMIUM_EXECUTABLE`을 지정하세요. |
| `arXiv ... failed: 503` | arXiv가 잠시 과부하 상태입니다. 다음 실행 때 다시 가져옵니다. |
| 저널 논문이 모두 "초록만 읽음" | 오픈액세스 PDF가 없거나 4번의 PDF 호스트가 막혀 있습니다. 정상 동작입니다. |
| 같은 논문이 두 번 리뷰됨 | gunicorn 워커가 여러 개입니다. `-w 1`로 바꾸세요. |
| (Heroku) 로그에 `R14 - Memory quota exceeded` | Chrome과 PDF 처리 중 메모리가 부족합니다. Scheduler 작업의 dyno 크기를 Standard-2X로 올리세요. |
| (Heroku) `card rendering failed: Could not launch Chromium` | 빌드팩 순서를 확인하세요 (`heroku buildpacks`: chrome-for-testing → python). 바꾼 뒤 다시 배포해야 합니다. |
| (Heroku) 12시간이 지났는데 실행이 안 됨 | Scheduler 작업이 `python -m paperagent run --if-due`로, Every hour로 등록됐는지 확인하세요. `heroku logs --ps scheduler`로 로그를 볼 수 있습니다. |
| (Heroku) 재배포 후 카드 이미지가 사라짐 | `DATABASE_URL`이 없어서 로컬 디스크에 저장되고 있었던 경우입니다. Postgres 애드온을 붙이고 해당 논문의 "카드 다시 만들기"를 누르세요. |
| 트렌드 패널이 비어 있음 | 첫 실행이 끝나야 채워집니다. "리뷰한 논문의 인용 변화"는 이틀 이상 기록이 쌓여야 표시됩니다. |
