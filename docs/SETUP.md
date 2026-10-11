# STAR WARS 설치·설정 가이드

코드는 그대로 두고, 운영하는 사람이 직접 준비해야 하는 것만 순서대로 정리했습니다.
처음 설치한다면 1번부터 6번까지 순서대로 진행하면 됩니다.

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

### 2-3. 비용 감 잡기

기본 모델은 `claude-opus-5-5`입니다. 입력 100만 토큰당 $4, 출력 100만 토큰당 $20입니다. 아래는 어림값입니다.

| 작업 | 토큰 (대략) | 비용 (대략) |
|---|---|---|
| 논문 1편 전문 리뷰 (20쪽 PDF) | 입력 4만~6만 + 출력 5천~1만 | 약 $0.3~0.45 |
| 초록 스크리닝 100편 | 입력 4만~5만 + 출력 수천 | 약 $0.2~0.3 |
| **기본 설정 1회 실행** (정독 5편) | | **약 $2 안팎** |
| 기본 설정 하루 (6시간마다 4회) | | **약 $8 안팎** |

비용을 줄이는 방법은 세 가지입니다.
- **정독 수와 실행 간격 조정:** 대시보드 설정 페이지에서 "한 번에 정독할 논문" 수를 줄이거나 "실행 간격"을 늘립니다. 예를 들어 하루 1회, 3편으로 하면 하루 약 $1.5입니다.
- **최소 관련도 올리기:** 기준을 6에서 7로 올리면 정독할 논문 수 자체가 줄어듭니다.
- **스크리닝만 저렴한 모델로:** `.env`에 `SCREEN_MODEL=claude-haiku-5-5`를 넣으면 초록 판단 비용이 크게 줄어듭니다. 리뷰 품질에는 영향이 없습니다.

`CLAUDE_EFFORT`(기본 `high`)를 `medium`으로 낮추면 리뷰 비용이 줄지만, 평가의 깊이도 다소 얕아집니다.

### 2-4. 키가 잘 동작하는지 확인

토큰을 쓰지 않는 확인 명령입니다.

```bash
python -c "import anthropic; print(anthropic.Anthropic().models.retrieve('claude-opus-5-5').display_name)"
```

모델 이름이 출력되면 성공입니다. `.env`를 쓰고 있다면 `set -a; source .env; set +a`를 먼저 실행하세요. 앱 자체는 `.env`를 자동으로 읽습니다.

### 2-5. AWS Bedrock·Google Vertex 등으로 Claude를 쓰는 경우

이 프로젝트는 Anthropic API를 직접 쓰도록 만들어져 있습니다. 다른 플랫폼을 쓰려면 `paperagent/agent.py`의 클라이언트 생성 부분을 해당 플랫폼용으로 바꾸고, `.env`에 `CLAUDE_FALLBACKS=0`을 넣으세요. 거절 시 자동 대체 모델 기능은 Anthropic API에서만 지원됩니다.

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

## 6. 상시 운영 (서버 재부팅에도 계속 돌게)

### 6-1. systemd 서비스

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

### 6-2. 외부에서 접속 가능하게

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

## 7. 데이터와 백업

| 경로 | 내용 | 크기 |
|---|---|---|
| `data/paperagent.db` | 논문 목록, 리뷰, 설정, 실행 기록 | 작음 (수 MB) |
| `data/figures/<id>/` | 논문에서 추출한 그림 PNG (카드 재생성용) | 논문당 약 1~3MB |
| `data/cards/<id>/` | 카드뉴스 PNG 6장, 캡션 | 논문당 약 2~4MB |

- PDF는 저장하지 않으므로 디스크가 빠르게 늘지 않습니다.
- 백업은 `data/` 폴더만 복사하면 됩니다. 다른 위치에 두려면 `DATA_DIR`을 지정하세요.
- 오래된 그림과 카드를 지워도 리뷰 텍스트는 DB에 남습니다. 그 논문의 카드를 다시 만들려면 "다시 읽기"를 누르세요.

---

## 8. 인스타그램 업로드

현재는 **수동 업로드**입니다.
1. 논문 상세 페이지에서 **전체 받기**를 눌러 카드 6장과 `caption.txt`가 든 ZIP을 받습니다.
2. **캡션 복사**를 누르고 인스타에 붙여 넣습니다.

자동 게시는 Instagram Graph API로 만들 수 있습니다. 다만 비즈니스 또는 크리에이터 계정, Facebook 페이지 연결, Meta 앱 등록과 권한 심사가 필요해서 이번 범위에는 넣지 않았습니다. 이미지는 공개 URL로 접근할 수 있어야 하는데, 대시보드의 `/files/card/<id>/<n>` 주소를 그대로 쓸 수 있습니다.

---

## 9. 문제 해결

| 증상 | 원인과 해결 |
|---|---|
| 실행 기록에 `401` / `authentication_error` | API 키가 틀렸거나 비어 있습니다. `.env`의 `ANTHROPIC_API_KEY`를 확인하고 서비스를 재시작하세요. |
| `400`에 credit / billing 관련 메시지 | 크레딧이 없거나 결제 수단이 없습니다. 콘솔 결제 메뉴에서 확인하세요. |
| `429` / `rate_limit_error` | 짧은 시간에 요청이 너무 많습니다. 자동으로 재시도합니다. 자주 나오면 "한 번에 정독할 논문" 수를 줄이세요. |
| `card rendering failed: Could not launch Chromium` | `python -m playwright install --with-deps chromium`을 다시 실행하거나 `CHROMIUM_EXECUTABLE`을 지정하세요. |
| `arXiv ... failed: 503` | arXiv가 잠시 과부하 상태입니다. 다음 실행 때 다시 가져옵니다. |
| 저널 논문이 모두 "초록만 읽음" | 오픈액세스 PDF가 없거나 4번의 PDF 호스트가 막혀 있습니다. 정상 동작입니다. |
| 같은 논문이 두 번 리뷰됨 | gunicorn 워커가 여러 개입니다. `-w 1`로 바꾸세요. |
| 트렌드 패널이 비어 있음 | 첫 실행이 끝나야 채워집니다. "리뷰한 논문의 인용 변화"는 이틀 이상 기록이 쌓여야 표시됩니다. |
