# 처음 한 번 하는 설정

이 문서는 **처음 한 번만** 하면 되는 준비 과정입니다.

- 내 컴퓨터에서 실행해 보기, 명령어, Heroku에 수정 사항 올리기 → [README](../README.md)
- API 키 발급, 비용, 환경 변수, Heroku 첫 배포, 문제 해결 → 이 문서

## 목차

1. [준비물 체크리스트](#1-준비물-체크리스트)
2. [Anthropic API 키와 비용](#2-anthropic-api-키와-비용)
3. [환경 변수 전체 목록](#3-환경-변수-전체-목록)
4. [Heroku 첫 배포](#4-heroku-첫-배포)
5. [인스타그램 업로드](#5-인스타그램-업로드)
6. [문제 해결](#6-문제-해결)

---

## 1. 준비물 체크리스트

| 항목 | 필요한 곳 | 어디서 | 비고 |
|---|---|---|---|
| Python 3.10 이상 | 내 컴퓨터 | [python.org](https://www.python.org/downloads/) | Heroku는 `.python-version`(3.12)을 씁니다 |
| Git | 내 컴퓨터 | [git-scm.com](https://git-scm.com/) | |
| **Anthropic API 키** | 실제 논문 리뷰 | [Claude Console](https://platform.claude.com) | 데모 데이터만 볼 때는 필요 없음 |
| 결제 수단 또는 크레딧 | 실제 논문 리뷰 | Claude Console 결제 메뉴 | |
| Heroku 계정과 CLI | 배포 | [heroku.com](https://www.heroku.com/), [Heroku CLI](https://devcenter.heroku.com/articles/heroku-cli) | |
| 인스타 계정명 | 카드뉴스 | 본인 계정 | `CARD_HANDLE` |

---

## 2. Anthropic API 키와 비용

### 2-1. 키 발급

1. [Claude Console](https://platform.claude.com)에 가입하거나 로그인합니다.
2. **Settings → API Keys**에서 **Create Key**를 누릅니다. 이름은 `star-wars`처럼 용도를 알 수 있게 짓습니다.
3. 만들어진 키(`sk-ant-...`)는 이때 **한 번만** 보이니 바로 복사해 둡니다.
4. 결제(Billing) 메뉴에서 결제 수단을 등록하거나 크레딧을 충전합니다. 잔액이 없으면 호출이 거절됩니다.
5. **월 사용 한도(spend limit)를 꼭 걸어 두세요.** 설정을 잘못 넣어도 한도 이상은 청구되지 않습니다.

> [!WARNING]
> API 키는 비밀번호와 같습니다. `.env` 파일이나 Heroku Config Vars에만 넣고, 코드나 GitHub에는 절대 올리지 마세요. `.env`는 `.gitignore`에 들어 있어서 실수로 커밋되지 않습니다.

### 2-2. 키 확인

토큰을 쓰지 않는 확인 명령입니다. 모델 이름 세 개가 출력되면 성공입니다.

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # Windows PowerShell: $env:ANTHROPIC_API_KEY="sk-ant-..."
python -c "import anthropic; c = anthropic.Anthropic(); [print(c.models.retrieve(m).display_name) for m in ('claude-haiku-5-5', 'claude-opus-5-5', 'claude-sonnet-5-5')]"
```

### 2-3. 단계별 모델과 비용

논문 한 편을 세 단계로 나눠, 각 단계에 꼭 필요한 만큼의 모델만 씁니다. 비싼 Opus는 PDF 전체가 아니라 정독 모델이 정리한 짧은 노트만 읽습니다.

| 단계 | 하는 일 | 기본 모델 | 가격 (100만 토큰당, 입력/출력) | 논문 1편 (20쪽 기준, 대략) |
|---|---|---|---|---|
| 관련도 판단 | 제목·초록으로 0~10점 | Haiku 5.5 | $0.10 / $0.50 | 실행 1회 전체 약 $0.01 |
| 정독 | PDF 전문을 읽고 근거 노트 작성 | Haiku 5.5 | $0.10 / $0.50 | 약 $0.01 |
| 평가 | 노트로 방법론·참신성 채점 | Opus 5.5 | $4 / $20 | 약 $0.09~0.15 |
| 작성 | 한국어 리뷰와 카드뉴스 문구 | Sonnet 5.5 | $2 / $10 | 약 $0.05~0.07 |

| 설정 | 논문 1편 | 하루 |
|---|---|---|
| **기본값** (12시간마다, 정독 3편, 관련도 7 이상) | 약 $0.15~0.22 | **약 $1~1.4** |
| 절약 설정 (아래 두 줄 추가) | 약 $0.08~0.12 | 약 $0.6 |

```bash
JUDGE_EFFORT=medium            # 평가 모델이 생각하는 양을 줄임 (점수 품질은 약간 내려갈 수 있음)
WRITER_MODEL=claude-haiku-5-5  # 한국어 문장이 약간 단조로워질 수 있음
```

실제로 쓴 비용은 대시보드의 **설정 → 모델과 비용**(최근 30일 합계, 논문당 평균), **실행 기록**(실행별), **논문 상세 페이지**(리뷰별)에서 볼 수 있습니다.

---

## 3. 환경 변수 전체 목록

- **로컬:** `.env` 파일에 넣습니다. `cp .env.example .env`로 만든 뒤 고치면 됩니다.
- **Heroku:** `heroku config:set 이름=값`으로 넣거나, 대시보드의 **Settings → Config Vars**에서 넣습니다.

| 이름 | 필수 | 기본값 | 설명 |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | ✅ | | Claude API 키 |
| `SECRET_KEY` | ✅ | `change-me` | 로그인 세션·CSRF용 비밀값. `python -c "import secrets; print(secrets.token_urlsafe(48))"`로 생성 |
| `ADMIN_PASSWORD` | 공개 시 ✅ | (없음) | 설정 페이지·관리 기능 로그인 비밀번호. 비우면 이 컴퓨터(127.0.0.1)에서만 관리자 |
| `OPENALEX_MAILTO` | 권장 | | 본인 이메일. 저널 조회가 더 안정적 |
| `SEMANTIC_SCHOLAR_API_KEY` | | | 인용 추적용. [신청 폼](https://www.semanticscholar.org/product/api)에서 발급 |
| `CARD_HANDLE` | 권장 | `@starwars.research` | 카드뉴스에 찍히는 인스타 계정 |
| `CARD_BASE_HASHTAGS` | | `#인공지능,#AI,#논문리뷰` | 캡션에 항상 붙는 해시태그 (쉼표로 구분). 나머지는 AI가 논문 분야에 맞게 고릅니다 |
| `CARD_HASHTAG_LIMIT` | | `5` | 캡션 해시태그 최대 개수. 인스타그램은 2025년 12월부터 게시물당 5개까지만 허용합니다 |
| `CARD_BRAND` | | `STAR WARS` | 카드뉴스 로고 글자 |
| `CARD_TECH_BASE_HASHTAGS` | | `#IT,#테크,#개발자` | 테크 카드뉴스 캡션에 항상 붙는 해시태그 |
| `TECH_SOURCES` | | `github,hn,geeknews,hf` | 테크 소식을 가져올 곳의 처음 값 (이후에는 **설정 → 테크 소식**에서 바꿉니다) |
| `TECH_PER_RUN` | | `2` | 실행마다 만드는 테크 카드뉴스 수의 처음 값. 0이면 정기 실행에서 만들지 않습니다 |
| `TECH_MIN_INTEREST` | | `7` | 테크 소식을 카드뉴스로 만들 최소 흥미도(0~10)의 처음 값 |
| `GITHUB_TOKEN` | | | 선택. GitHub Trending 페이지를 못 읽을 때 쓰는 GitHub 검색 API의 호출 한도를 늘립니다 (권한 없는 토큰이면 충분) |
| `VIDEO_CARDS` | | `1` | `0`이면 마지막 장 영상(MP4)을 만들지 않고 모두 이미지 카드로 만듭니다 |
| `FFMPEG_EXECUTABLE` | | | 영상 카드에 쓸 ffmpeg 경로. 비우면 시스템 ffmpeg, 없으면 `imageio-ffmpeg` 패키지에 들어 있는 것을 씁니다 (Heroku는 따로 설치할 필요 없음) |
| `SITE_NAME` | | `STAR WARS` | 대시보드 이름 |
| `READER_MODEL` | | `claude-haiku-5-5` | PDF 정독 모델 |
| `JUDGE_MODEL` | | `claude-opus-5-5` | 채점 모델 |
| `WRITER_MODEL` | | `claude-sonnet-5-5` | 한국어 리뷰·카드 문구 모델 |
| `SCREEN_MODEL` | | `claude-haiku-5-5` | 관련도 판단 모델 |
| `JUDGE_EFFORT` / `READER_EFFORT` / `WRITER_EFFORT` | | `high` / `low` / `low` | 모델이 생각하는 양 (`low`·`medium`·`high`) |
| `FETCH_INTERVAL_HOURS` | | `12` | 실행 간격 (처음 기본값. 이후엔 대시보드 설정이 우선) |
| `MAX_FULL_READS` | | `3` | 한 번에 정독할 논문 수. 최소 관련도 이상인 논문 중 관련도 높은 순으로 최대 이만큼 읽습니다 (위와 같음) |
| `MIN_RELEVANCE` | | `7` | 정독할 최소 관련도 (위와 같음) |
| `LOOKBACK_DAYS` / `MAX_PER_SOURCE` | | `3` / `40` | 수집 기간, 소스당 최대 수집 수 |
| `DATABASE_URL` | Heroku 자동 | (로컬 SQLite) | Postgres 주소. Heroku Postgres 애드온이 자동으로 넣음 |
| `STORAGE_BACKEND` | | 자동 | `local`(파일) 또는 `db`. `DATABASE_URL`이 있으면 `db` |
| `SCHEDULER_ENABLED` | | 로컬 `1`, Heroku `0` | 앱 안의 자동 실행 타이머 |
| `DATA_DIR` | | `./data` | 로컬 DB와 이미지 저장 위치 |
| `DISPLAY_TIMEZONE` | | `Asia/Seoul` | 화면과 실행 기록에 쓰는 시간대 (DB에는 UTC로 저장) |
| `CHROMIUM_EXECUTABLE` | | 자동 탐색 | Chrome·Chromium 경로를 직접 지정할 때만 |
| `CLAUDE_FALLBACKS` | | `1` | 거절 시 자동 대체 모델 (Anthropic API 전용) |

---

## 4. Heroku 첫 배포

처음 한 번만 하면 됩니다. 이후 수정 사항을 올리는 방법은 [README의 Heroku 업데이트](../README.md#5-heroku에-변경-사항-올리기)를 보세요.

> [!NOTE]
> 저장소에 Heroku용 파일이 이미 들어 있습니다.
> - `Procfile`: 웹 서버 실행과, 배포 때 DB를 준비하는 release 명령
> - `app.json`: 애드온·빌드팩·환경 변수 목록
> - `.python-version`: Python 버전
> - `.slugignore`: 테스트·문서를 배포 용량에서 제외

### 4-1. Heroku CLI 설치와 로그인

```bash
# macOS
brew tap heroku/brew && brew install heroku
# Windows: https://devcenter.heroku.com/articles/heroku-cli 에서 설치 파일 실행
# Ubuntu
curl https://cli-assets.heroku.com/install-ubuntu.sh | sh

heroku login            # 브라우저가 열리면 로그인
heroku --version        # 설치 확인
```

### 4-2. 앱 만들기

저장소 폴더 안에서 실행합니다.

```bash
cd STAR_WARS
heroku create my-starwars      # 앱 이름은 원하는 대로. 이미 있는 이름이면 다른 이름으로
git remote -v                  # heroku 라는 원격 저장소가 추가됐는지 확인
```

이미 만든 앱을 다른 컴퓨터에서 연결할 때는 `heroku git:remote -a my-starwars`를 실행합니다.

### 4-3. 빌드팩 (순서 중요)

```bash
heroku buildpacks:add -i 1 https://github.com/heroku/heroku-buildpack-chrome-for-testing
heroku buildpacks:add -i 2 heroku/python
heroku buildpacks          # 1. chrome-for-testing  2. heroku/python 순서인지 확인
```

Chrome은 카드뉴스를 PNG로 찍을 때 씁니다.

### 4-4. 애드온

```bash
heroku addons:create heroku-postgresql:essential-0   # DB (DATABASE_URL 자동 설정)
heroku addons:create scheduler:standard              # 12시간마다 실행
heroku config:get DATABASE_URL                       # postgres://... 가 나오면 성공
```

> [!IMPORTANT]
> Heroku의 디스크는 재시작할 때마다(하루 1회 이상) 지워집니다. 그래서 논문·리뷰는 물론 카드뉴스 이미지까지 모두 Postgres에 저장합니다. `DATABASE_URL`이 있으면 코드가 자동으로 그렇게 동작합니다.

### 4-5. 환경 변수

```bash
heroku config:set \
  ANTHROPIC_API_KEY=sk-ant-... \
  SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))") \
  ADMIN_PASSWORD='긴-비밀번호' \
  OPENALEX_MAILTO=you@example.com \
  CARD_HANDLE=@실제_인스타_계정
heroku config        # 들어간 값 확인
```

Windows PowerShell에서는 줄바꿈 `\` 대신 한 줄로 쓰고, `SECRET_KEY`는 먼저 만든 값을 붙여 넣으세요.

### 4-6. 첫 배포

```bash
git push heroku main
```

> [!TIP]
> 지금 작업 중인 브랜치가 `main`이 아니라면 `git push heroku 내-브랜치:main`처럼 보냅니다.
> Heroku는 자기 쪽 `main`(또는 `master`)에 들어온 코드만 배포합니다.
> 예: `git push heroku claude/blissful-heisenberg-ybixq0:main`

배포 로그 끝에 아래 두 가지가 보이면 성공입니다.
- `Running release command... database ready`
- `https://my-starwars-xxxx.herokuapp.com/ deployed to Heroku`

### 4-7. 스케줄러 등록 (한 번만)

```bash
heroku addons:open scheduler
```

열린 화면에서 **Create job**(또는 **Add Job**)을 누르고 아래처럼 입력한 뒤 **Save Job**을 누릅니다.

| 항목 | 값 |
|---|---|
| Schedule | **Every hour at :00** |
| Run Command | `python -m paperagent run --if-due` |
| Dyno size | **Standard-2X** 권장 (Chrome과 PDF 처리에 메모리 1GB). Basic(512MB)도 동작하지만 메모리 경고(R14)가 날 수 있습니다. |

매시간 잠깐 깨어나서 "지난 실행 후 12시간이 지났는지" 확인하고, 지났을 때만 수집과 리뷰를 합니다. 간격은 대시보드 설정 페이지에서 바꾸면 바로 반영됩니다.

### 4-8. 확인

```bash
heroku open                                           # 대시보드 열기
heroku run python -m paperagent demo                  # (선택) 데모 데이터로 화면 확인
heroku run python -m paperagent review 1706.03762     # 실제 논문 한 편으로 전체 흐름 확인 (1~3분)
heroku logs --tail                                    # 로그 보기 (Ctrl+C로 종료)
```

데모 데이터는 확인한 뒤 `heroku run python -m paperagent demo --clear`로 지우세요.

### 4-9. Heroku 비용과 용량

| 항목 | 대략 (정확한 금액은 [Heroku 요금](https://www.heroku.com/pricing)에서 확인) |
|---|---|
| 웹 dyno (Basic) | 월 약 $7 |
| Heroku Postgres (Essential-0, 1GB) | 월 약 $5 |
| Scheduler 실행 dyno | 쓴 시간만큼 (하루 몇 분, 월 $1 미만) |

카드뉴스와 그림은 논문 한 편에 2~4MB 정도 DB를 씁니다. 하루 6편이면 1GB를 두 달쯤에 채우므로, 오래 운영할 계획이라면 Essential-1 이상으로 올리세요.

### 4-10. 백업

```bash
heroku pg:backups:schedule DATABASE_URL --at '04:00 Asia/Seoul'   # 매일 자동 백업
heroku pg:backups:capture                                         # 지금 백업
heroku pg:backups:download                                        # 내려받기
```

---

## 5. 인스타그램 업로드

게시는 직접 합니다. 대시보드는 올릴 재료를 한 번에 준비해 줍니다.

1. 논문(또는 테크 소식) 페이지의 카드뉴스 칸에서 **전체 받기**를 누르면 카드 이미지, 마지막 장 영상(`.mp4`, 만들 수 있을 때)와 `caption.txt`가 든 ZIP이 내려옵니다. 파일 이름의 번호가 올릴 순서입니다.
2. 인스타 앱에서 새 게시물 → 여러 장 선택으로 번호 순서대로 고릅니다. 사진과 영상을 한 게시물에 섞어 올릴 수 있습니다(최대 20장, 영상은 장당 60초 이하).
3. **캡션 복사**를 눌러 캡션을 붙여 넣습니다. 해시태그는 인스타 제한(게시물당 5개)에 맞춰 들어 있습니다.

## 6. 문제 해결

| 증상 | 원인과 해결 |
|---|---|
| 화면 위에 "Claude API를 쓸 수 없어 AI 리뷰가 멈춰 있습니다" | 안내 문구에 원인(키 없음, 잘못된 키, 크레딧 부족 등)이 나옵니다. 키를 고친 뒤 **API 다시 확인**을 누르세요. 그동안에도 수집·트렌드·기존 리뷰는 정상입니다. |
| 실행 기록 상태가 "AI 중단" | 위와 같은 API 문제로 AI 단계만 건너뛴 실행입니다. 다음 실행(Heroku는 매시간 확인)에서 자동으로 다시 시도합니다. |
| 실행 기록에 `401` / `authentication_error` | API 키가 틀렸거나 비어 있습니다. 로컬은 `.env`, Heroku는 `heroku config`로 확인하세요. |
| `400`에 credit / billing 관련 메시지 | 크레딧이나 결제 수단이 없습니다. 콘솔 결제 메뉴를 확인하세요. |
| `429` / `rate_limit_error` | 요청이 몰렸습니다. 자동으로 재시도합니다. 자주 나오면 "한 번에 정독할 논문" 수를 줄이세요. |
| `card rendering failed: Could not launch Chromium` | 로컬: `python -m playwright install chromium`을 다시 실행. Heroku: `heroku buildpacks` 순서(chrome-for-testing → python)를 확인한 뒤 다시 배포. |
| `arXiv ... failed: 503` | arXiv가 잠시 과부하 상태입니다. 다음 실행 때 다시 가져옵니다. |
| 저널 논문이 모두 "초록만 읽음" | 오픈액세스 PDF가 없는 논문입니다. 정상 동작입니다. |
| 업로드한 PDF의 제목이 이상함 | PDF 안의 정보로 제목을 추측합니다. 올릴 때 제목 칸에 직접 적으면 그 제목을 씁니다. 업로드한 PDF는 리뷰가 끝나면 서버에서 지웁니다. |
| 학회(NeurIPS 등)를 검색해도 결과가 없음 | 잘 알려진 AI·CS 학회와 저널은 약칭(NeurIPS, ICML, ICLR, CVPR, ACL, EMNLP, AAAI, KDD, TPAMI 등)으로 바로 찾아집니다. 그 밖의 곳은 정식 이름으로 검색하거나, 결과가 없을 때 **이름으로 추가**를 누르면 다음 실행 때 OpenAlex에서 다시 찾습니다. |
| 학회 논문이 안 모임 | 실행 기록에서 `NeurIPS: n matched (arXiv n, OpenAlex n)` 줄을 보세요. 학회는 arXiv에 채택 사실이 적힌 논문과 OpenAlex 목록에서 최근 400일치를 찾고, 관심 키워드에 맞는 논문만 가져옵니다. 이미 모은 논문은 "0 new"로 나옵니다. |
| 같은 논문이 두 번 리뷰됨 | gunicorn 워커가 여러 개입니다. `Procfile`의 `-w 1`을 바꾸지 마세요. |
| (Heroku) `R14 - Memory quota exceeded` | Scheduler 작업의 dyno 크기를 Standard-2X로 올리세요. |
| (Heroku) 12시간이 지났는데 실행이 안 됨 | Scheduler 작업이 `python -m paperagent run --if-due`, Every hour로 등록됐는지 확인하세요. `heroku logs --tail`에서 `app[scheduler.…]`이 붙은 줄이 실행 로그입니다. |
| (Heroku) `H10 - App crashed` | `heroku logs --tail`에서 원인을 보세요. 대부분 환경 변수 누락이나 빌드팩 순서 문제입니다. |
| 대시보드 상단에 "논문 읽는 중"이 계속 떠 있음 | "논문 읽는 중"을 눌러 실행 콘솔을 열면 어느 단계에서 멈췄는지 로그로 볼 수 있습니다. 관리자는 콘솔이나 대기열의 **강제 종료**로 바로 멈출 수 있고, 읽던 논문은 대기열로 돌아갑니다. 응답 없이 3분 이상 멈춘 Claude 요청은 자동으로 끊고 다시 시도합니다. 프로세스가 아예 꺼진 경우는 3시간 뒤 자동으로 정리됩니다. |
| 트렌드 패널이 비어 있음 | 관리자로 트렌드 페이지의 **지금 새로고침**을 누르세요 (API 키 불필요). "리뷰한 논문의 인용 변화"는 이틀 이상 기록이 쌓여야 보입니다. |
| 설정 메뉴가 안 보임 | 관리자만 보입니다. `ADMIN_PASSWORD`를 설정하고 **관리자 로그인**을 하세요. |
