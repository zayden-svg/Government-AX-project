# 공공 IT Insight — Government-AX-project

기존 Python·Streamlit 프로젝트를 정비한 공공 IT 공고 수집·Gemini 분석 플랫폼입니다. 새 프로젝트로 교체하지 않고 수집기·화면·실행 경로를 연결했습니다.

## 2026-09-10 현재 검증 결과와 제한

- 기존 SQLite 공고 84건을 보존하고 별도 원문 테이블로 이관했습니다. 과거의 검증되지 않은 AI 점수·제품 추천은 새 화면에서 사용하지 않습니다.
- NIA·나라장터에서 각 2건 실제 수집·저장을 확인했습니다. 같은 4건을 다시 수집했을 때 신규·갱신 0건, 동일 4건이었습니다.
- NIA 공고 1건의 실제 Gemini 분석·원문 인용 검증·DB 저장을 확인했습니다.
- 이후 Google이 기존 Gemini 키를 `403 PERMISSION_DENIED`로 차단했습니다. 응답은 `Your API key was reported as leaked. Please use another API key.`입니다. 키는 사용자 요청대로 바꾸지 않았으며 추가 호출을 중단했습니다. 유료 티어 여부와 별개로 새 서버 키가 필요합니다.
- 자동 테스트 36개(핵심 처리 31개, 화면 5개)를 로컬에서 통과했습니다. 실제 Supabase PostgreSQL 연결 시험은 계정 연결 전이므로 미완료입니다.
- 무료 운영 후보는 **Streamlit Community Cloud + Supabase Free + GitHub Actions**입니다. 실제 외부 서버·DB는 아직 생성·연결되지 않았습니다. 샌드박스 미리보기는 상시 운영 서버가 아닙니다.
- GitHub 연결 앱에 Actions 비밀값 설정 및 workflows 작성 권한이 없습니다. 자동 실행 설정은 `public-it.workflow.yml`이라는 **비활성 템플릿**으로 제공했습니다. 현재 예약 실행이 켜진 상태가 아닙니다.
- 현재 변경사항은 `genspark_ai_developer` 브랜치와 [PR #1](https://github.com/zayden-svg/Government-AX-project/pull/1)에 있습니다. main 반영은 별도 검토가 필요합니다.

## 제품 기준

| 제품 | 적용 기준 |
|---|---|
| NetFUNNEL | 온프레미스·SaaS 모두 지원 |
| NetFUNNEL API | 최신 API 제품소개서에 명시된 API 대기열·우선순위·응답시간·메트릭 제어 기능을 참조 |
| BotManager | 현재 판매 중인 SaaS 제품 |
| MBUSTER | BotManager 온프레미스의 예정 제품명. 2026년 말 제품화·판매 예정, 출시 확인 전 현재 판매 추천 금지 |

`product_profile.py`가 유일한 제품 판단 기준입니다. 구 MBUSTER PDF의 기능·성능·낙찰 사례와 기존 키워드 분류표는 분석에서 제외했습니다. 해당 구 자료를 재생성하는 스크립트와 엑셀도 현재 브랜치에서 제거했습니다. 사용자 업로드 PDF 원본이나 과거 Git 저장 이력을 영구 삭제한 것은 아닙니다.

## 처리 구조

```text
collectors.py (기관별 수집 함수)
    → main.py (단일 실행 경로)
    → db.py (변경 전 원문 보존, 중복 방지)
    → ai_utils.py (Gemini만 사용, 제품 기준 + 원문 인용 검증)
    → db.py (분석 결과·모델·제품 기준 버전·작업 이력 저장)
    → app.py (원문 / AI 분석 / AI 추정 분리 표시)
```

- `main_local.py`, `gov_tracker.py`, `app_local.py`, `db2.py`는 공통 구현을 사용하는 호환 진입점입니다.
- 키워드로 R&D·사업부·높은 관련도를 확정하지 않습니다. 본문이 부족하면 분석을 보류합니다.
- `notices_v2`, `originals_v2`, `analyses_v2`, `jobs_v2`, `locks_v2`, `ai_attempts_v2` 테이블을 사용합니다.
- PostgreSQL에서는 외부 공개 API의 기본 영역과 분리된 `govtracker` 스키마를 사용합니다. 행 접근 잠금도 활성화합니다. 서버에만 DB 소유자 연결정보를 두고 사용자 브라우저에 전달하지 않습니다.
- 같은 공고번호의 정정은 새 원문 버전으로 저장합니다. 공고번호가 없을 때는 기관·제목·게시일·주소를 함께 사용하여 같은 목록 주소 때문에 다른 공고가 덮어써지지 않도록 합니다.
- 서로 다른 기관의 같은 사업을 자동으로 합치는 고급 중복 판단은 아직 추가 검증이 필요합니다.
- 원문·제품 기준·분석 모델·지시문 버전이 바뀌면 예전 분석을 최신 결과인 것처럼 표시하지 않습니다.

## 로컬 실행 — Windows

기존 폴더를 통째로 삭제하지 마세요. `.env`와 `gov_tracker.db`를 먼저 따로 안전하게 백업하세요. 배포용 소스 ZIP에는 키·DB·가상환경이 포함되지 않습니다.

프로젝트 폴더에서 터미널을 열어 순서대로 실행합니다.

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe main.py --import-legacy
.venv\Scripts\python.exe main.py --sources NIA,조달청 --limit 2 --no-ai
.venv\Scripts\python.exe -m streamlit run app.py
```

정상 결과: 터미널에 수집 건수가 나오고 `http://localhost:8501`에서 화면이 열립니다. `--no-ai`는 차단된 Gemini 키 없이 수집만 검증하는 옵션입니다.

기존 DB가 없는 새 설치라면 `--import-legacy` 결과는 0건이며 정상입니다. 새 DB는 수집부터 시작합니다.

## 로컬 실행 — Linux / macOS

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py --import-legacy
.venv/bin/python main.py --sources NIA,조달청 --limit 2 --no-ai
.venv/bin/python -m streamlit run app.py
```

## 환경변수 설정

환경변수는 서버만 볼 수 있는 별도의 설정 보관함입니다. 키를 Python 코드나 GitHub 파일에 붙여넣지 않습니다.

새 설치에서는 `.env.example`을 `.env`라는 이름으로 복사합니다. **이미 `.env`가 있으면 예제로 덮어쓰지 마세요.** 설정 파일은 메모장에서 UTF-8로 저장할 수 있습니다.

각 설정의 의미:

| 이름 | 의미 |
|---|---|
| GEMINI_API_KEY | 사용 가능한 Gemini 키. 현재 보존된 키는 Google에 의해 차단됨 |
| G2B_SERVICE_KEY | 기존 나라장터 인증키. 실제 수집 성공 확인 |
| GEMINI_MODEL | 기본값 gemini-2.5-flash |
| DATABASE_URL | Supabase PostgreSQL 연결 주소. 비워두면 로컬 SQLite 사용 |
| ADMIN_PASSWORD | 웹에서 최신 수집·분석을 실행하는 운영자 비밀번호 |
| ENABLED_COLLECTORS | 기본 NIA,조달청. 다른 기존 수집기는 실사이트 검증 후 활성화 |
| COLLECTION_LIMIT | 실행당 기관별 최대 공고 수. 기본 10, 최대 25 |
| MAX_ANALYSES_PER_RUN | 실행당 분석 상한. 기본 5 |
| MAX_ANALYSES_PER_DAY | 한국시간 하루 요청 상한. 기본 50. 실패 요청도 집계 |

차단 해제된 키를 안전하게 서버에 등록한 이후에만 다음 명령으로 분석합니다.

```bash
python main.py --analyze-only
```

인증 실패·호출 한도 초과는 같은 실행에서 반복하지 않습니다. 실패한 공고는 원문을 보존한 상태로 다음 실행의 분석 대상으로 남깁니다.

## 무료 DB: Supabase Free

현재는 연결 계정·DB 주소가 없어 실제 생성할 수 없었습니다. 사용자가 한 번 설정해야 합니다.

1. https://supabase.com 에서 로그인합니다.
2. 무료 Free 요금제로 프로젝트를 생성합니다. 별도 유료 옵션은 선택하지 않습니다.
3. Connect 화면에서 PostgreSQL **Session pooler** 연결 주소를 복사합니다. 일반적으로 5432 포트를 사용하며 무료 환경의 IPv4 연결 문제를 피하기 위한 선택입니다.
4. 주소의 비밀번호 부분을 프로젝트 DB 비밀번호로 설정합니다. 특수문자가 있는 비밀번호는 URL에 맞는 인코딩이 필요합니다. 비밀번호를 채팅이나 GitHub 파일에 공개하지 마세요.
5. 연결 문자열에 TLS 암호화를 위한 `sslmode=require`를 사용합니다.
6. 이 전체 주소를 Streamlit Secrets와 GitHub Actions Secret의 `DATABASE_URL`에 넣습니다.

서버 최초 실행 시 필요한 테이블이 생성됩니다. Supabase 소유자 수준 DB 연결을 서버에서만 사용합니다. `govtracker` 스키마를 Data API의 공개 스키마에 추가하지 마세요.

공식 무료 조건 확인: https://supabase.com/pricing
- DB 500MB, 파일 저장소 1GB 등의 제한이 있습니다.
- 비활성 프로젝트 일시중지와 자동 백업 제한이 있어, 무료라고 무중단·영구 보존을 보장하지 않습니다.
- 원문과 첨부를 계속 축적하면 용량을 관리해야 합니다. 임의로 오래된 공고를 삭제하지 않습니다.

## 무료 웹 서버: Streamlit Community Cloud

1. PR의 변경 코드를 검토하고 main에 반영합니다.
2. https://share.streamlit.io 에서 GitHub 계정을 연결합니다.
3. Create app에서 저장소 `zayden-svg/Government-AX-project`, 브랜치 `main`, 실행 파일 `app.py`를 선택합니다.
4. Advanced settings 또는 앱 Settings의 Secrets에서 서버 설정을 등록합니다.
5. Deploy를 선택합니다.

Secrets 입력 형식 전체 예시입니다. **실제 값은 해당 비공개 입력창에서만 교체합니다. 저장소에 파일로 올리지 않습니다.**

```toml
DATABASE_URL = "여기에 Supabase Session pooler 연결 주소"
GEMINI_API_KEY = "여기에 사용 가능한 Gemini 키"
G2B_SERVICE_KEY = "여기에 기존 나라장터 키"
GEMINI_MODEL = "gemini-2.5-flash"
ADMIN_PASSWORD = "여기에 별도로 정한 긴 운영자 비밀번호"
ENABLED_COLLECTORS = "NIA,조달청"
MAX_ANALYSES_PER_RUN = "5"
MAX_ANALYSES_PER_DAY = "50"
```

정상 결과: 제공된 `streamlit.app` 주소에서 웹 화면이 열립니다. 운영자 비밀번호로 최신 수집을 실행하면 같은 Supabase DB에 새 결과가 저장됩니다. `.env`는 배포 저장소에 없으므로 반드시 Secrets 설정을 사용해야 합니다.

공식 무료 호스팅 안내: https://docs.streamlit.io/deploy/streamlit-community-cloud
앱이 쉬거나 자원 제한에 걸릴 수 있습니다. 상시 운영 보장 서비스가 아니라 초기 저비용 서비스입니다.

## 매일 오전 8시 자동 수집 활성화

**현재 `public-it.workflow.yml`은 템플릿일 뿐 실행되지 않습니다.** GitHub 연결 앱의 `workflows` 권한 부족으로 등록이 거부되어 사용자 등록을 위해 제공했습니다.

파일: `.github/workflows/public-it.yml`

작업:
1. GitHub 저장소의 main 브랜치를 엽니다.
2. Add file → Create new file을 선택합니다.
3. 파일 이름을 `.github/workflows/public-it.yml`로 입력합니다.
4. 저장소 루트의 `public-it.workflow.yml` 파일을 열고, **파일 전체를 처음부터 끝까지 복사**하여 새 파일에 붙여넣습니다. 같은 경로에 파일이 이미 있다면 먼저 기존 내용을 백업한 후 전체 교체합니다.
5. Commit changes로 저장합니다.
6. Settings → Secrets and variables → Actions → New repository secret에서 `DATABASE_URL`, `GEMINI_API_KEY`, `G2B_SERVICE_KEY`를 각각 비공개로 등록합니다.
7. Actions → Public IT collection and tests → Run workflow를 한 번 실행합니다.

정상 결과: test 단계 통과 → collect 단계 실행 → 웹 화면의 수집 현황에 실행 결과가 나타납니다.

예약식 `0 23 * * *`는 UTC 23시, 한국시간 다음 날 오전 8시입니다. **8시는 작업 시작 목표이며 결과 완료 보장 시각은 아닙니다.** 테스트·수집·AI 처리 시간이 추가로 필요합니다. 무료 GitHub 예약 실행은 지연·누락 가능성이 있고, 공개 저장소가 60일간 비활성이면 예약이 중지될 수 있습니다.

DB 설정이 없을 때 일회성 GitHub 실행기의 SQLite에 저장하고 성공한 척하지 않도록 실행을 차단합니다. 웹과 자동 수집은 반드시 동일한 Supabase DB 주소를 사용해야 합니다.

공식 안내:
- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
- https://docs.github.com/en/billing/concepts/product-billing/github-actions

## 테스트

```bash
python -m unittest test_core test_ui -v
```

정상 결과: `Ran 36 tests`와 `OK`가 표시됩니다. 테스트는 임시 DB와 가짜 API 응답을 사용하며 실제 유료 AI 요청이나 기존 DB 변경을 하지 않습니다.

검사 범위: 같은 목록 URL의 다른 공고, 원문 변경 이력, 중복 분석 방지, 미래 MBUSTER 추천 차단, 가짜 인용문·잘못된 제품 출처 거부, 기관 수집 실패 표시, 동시 실행 잠금, 일일 요청 제한, 화면의 DB 분류 일치, `[` 검색, 운영자 권한 없는 유료 실행 차단 등.

## 오류가 발생했을 때

| 화면/오류 | 확인할 사항 |
|---|---|
| Gemini 유출 키 차단 | Google이 키를 차단한 상태입니다. 같은 키 재시도로 해결되지 않습니다. 새 키를 비공개 서버 설정에 등록해야 합니다. |
| Gemini 형식·근거 검증 실패 | 결과를 정상 분석으로 저장하지 않습니다. 원문은 남아 있으며 재시도·프롬프트 점검 대상입니다. |
| DB 연결 실패 | DATABASE_URL, DB 비밀번호, Session pooler 주소, 프로젝트 일시중지 여부 확인 |
| 수집 실패 | 기관 사이트 장애·구조 변경·나라장터 API 승인 상태 확인. 0건 수집 성공과 구분합니다. |
| 본문 확보 필요 | 제목·목록만 확보된 자료입니다. 추정으로 분류하지 않습니다. |
| 운영자 버튼 비활성 | 서버에 ADMIN_PASSWORD를 설정하고 해당 비밀번호 입력 |
| 예약 작업이 안 보임 | 템플릿을 `.github/workflows/public-it.yml`로 main에 등록했는지 확인 |
| 같은 작업 진행 중 | 동시 실행 방지 잠금입니다. 정상 완료 시 해제되며 비정상 중단 시 최대 1시간 뒤 만료됩니다. |

## 남은 개발 범위

- HWP/HWPX/PDF 첨부 본문 추출 및 페이지별 근거 연결
- 나라장터 페이지 순회·증분 수집, 실제 발주기관별 지역·공고일·입찰 마감일 표준화
- 기관 간 동일 사업 연결, 정정·취소 공고의 고급 비교
- 추가 중앙부처·지자체 수집기 실사이트 검증
- 공개 검색 후 최신 수집 요청의 비동기 처리 및 더 세밀한 운영 권한
- 정확한 8시 완료가 필요한 경우 무료 예약 실행 대신 상시 스케줄러 검토
- 알림, 관심 키워드, 과거 사업 비교

## 보안과 기존 데이터

- 기존 `.env`의 키 값은 변경하지 않았습니다. 현재 추적 파일에서 키 값이 검출되지 않도록 검사했습니다.
- `.env`, 운영 DB, 결과 엑셀, 백업, 로그는 Git 추적에서 제외했습니다. 과거 커밋의 키를 삭제하거나 Git 이력을 강제로 다시 쓰지는 않았습니다. main에도 PR 반영 전의 파일이 남아 있을 수 있습니다.
- 기존 DB는 이관 전 `backups/gov_tracker_before_migration_2026-09-10.db`에 별도 백업했습니다. 이 경로는 작업 환경에만 있으며 공개 배포 파일에 포함되지 않습니다.
- 기존 `postings` 테이블은 수정·삭제하지 않습니다. 구 분석 내용은 새 분석 입력이나 화면에 재사용하지 않습니다.
- 원문 링크는 공식 공고 확인용입니다. AI 분석은 수주 가능성 보장이나 사실 확인을 대신하지 않습니다.
