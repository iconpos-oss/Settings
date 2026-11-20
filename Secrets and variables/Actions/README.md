# Blogger 실시간 트렌드 자동 게시 봇

매일 아침(KST 07:00) 구글 실시간 트렌드(대한민국)를 가져와 Blogger에 자동으로 글을 발행합니다.
GitHub Actions의 무료 크론잡을 사용하므로 별도 서버 비용이 들지 않습니다.

## 준비 단계

### 1. Blogger 블로그 ID 확인
블로그 관리 화면 URL에서 숫자로 된 ID를 확인합니다.
예: `blogger.com/blog/posts/6023330275183611634` → 이 앞부분 URL의 blogID 값
(블로그 설정 > 기본사항 에서도 확인 가능)

### 2. Google Cloud Console에서 OAuth 클라이언트 생성
1. https://console.cloud.google.com 에서 새 프로젝트 생성
2. "API 및 서비스 > 라이브러리"에서 **Blogger API v3** 검색 후 사용 설정
3. "API 및 서비스 > OAuth 동의 화면" 설정 (테스트 사용자에 본인 계정 추가)
4. "API 및 서비스 > 사용자 인증 정보 > 사용자 인증 정보 만들기 > OAuth 클라이언트 ID"
   - 애플리케이션 유형: **데스크톱 앱**
5. 생성된 JSON을 다운로드하여 `client_secret.json`으로 저장

### 3. 리프레시 토큰 발급 (내 컴퓨터에서 1회만 실행)
```bash
pip install google-auth-oauthlib
python get_refresh_token.py
```
브라우저가 열리면 블로그 계정으로 로그인 후 동의합니다.
터미널에 출력되는 `client_id`, `client_secret`, `refresh_token` 값을 복사해둡니다.

### 4. GitHub 저장소 준비
1. 이 폴더를 새 GitHub 저장소로 push
2. 저장소 > Settings > Secrets and variables > Actions 에서 다음 4개 등록:
   - `BLOGGER_BLOG_ID`
   - `GOOGLE_CLIENT_ID`
   - `GOOGLE_CLIENT_SECRET`
   - `GOOGLE_REFRESH_TOKEN`

### 5. 동작 확인
- Actions 탭 > "Daily Blogger Trend Post" > "Run workflow"로 수동 실행해 테스트
- 정상 동작하면 매일 KST 07:00에 자동으로 실행됩니다

## 커스터마이징
- `post_trends.py`의 `MAX_ITEMS`로 가져올 트렌드 개수 조절
- `build_post()` 함수에서 글 제목/본문 형식 자유롭게 수정
- 실행 시간을 바꾸려면 `.github/workflows/daily-trend-post.yml`의 cron 값 수정
  (cron은 UTC 기준이므로 KST -9시간으로 계산)

## 글 포맷 (2026-11-20 개선)
애드센스 심사와 가독성을 위해 본문을 아래 구조로 조립합니다. 모든 블록은 각각의
`<p>`/`<h3>`/`<li>`로 만들어져 문단과 줄바꿈이 분명히 나뉩니다.

1. **서론 2문단** — 오늘 날짜(KST)·검색어 개수·상위 3개 키워드 요약, 그리고
   검색 관심도 표기와 기사 요약 인용에 대한 안내
2. **오늘의 인기 검색어 목록** — 순위·키워드·관심도를 한눈에 보는 `<ul>` 목록
3. **순위별 상세** — 각 검색어마다
   - `<h3>순위. 키워드</h3>` 소제목
   - `<p><strong>검색 관심도</strong>: 500+ (구글 트렌드 집계 기준)</p>`
     (`ht:approx_traffic` 값, 없으면 '구글 트렌드 표시값 없음')
   - `<p>` 이슈 요약 2~3문장 — RSS `ht:news_item_snippet`을 우선 쓰고, 비어 있으면
     기사 원문의 메타 `og:description`/`description`만 읽어 정리합니다. 둘 다 없으면
     기사 제목·출처만으로 사실대로 안내합니다(없는 내용을 지어내지 않음).
   - `<p>관련 기사: <a target="_blank" rel="noopener noreferrer">제목 — 출처</a></p>`
     기사가 없으면 "관련 기사: 구글 트렌드가 연결한 기사 없음"과 포털 검색 안내
4. **마무리 2문단** — 순위 변동 안내, 자동 작성·저작권 고지

본문 값은 모두 외부 입력으로 취급합니다. 텍스트는 `html.escape`로 이스케이프하고,
링크는 `safe_url()`을 통과한 `http/https` URL만 씁니다(`javascript:` 등은 버림).
기사 원문은 요약용 메타 태그만 읽고 본문을 복사하지 않습니다.

- 요약 수집을 끄려면 `FETCH_DESCRIPTIONS=0`(또는 `--no-fetch`)을 씁니다. 테스트와
  오프라인 실행에서 유용합니다. 요약 수집은 전체 60초 예산 안에서만 시도하고,
  실패한 항목은 안내 문구로 대체해 발행을 막지 않습니다.
- 발행 전 본문을 눈으로 확인하려면:
  ```bash
  python "Secrets and variables/Actions/post_trends.py" --dry-run
  ```

## 티스토리(`essay10410.tistory.com`) 발행 시 포맷
티스토리는 Blogger에 발행된 글의 제목·본문 HTML을 그대로 가져와 올리므로, 위 구조가
자동으로 그대로 적용됩니다(2026-11-20 이후 발행분부터). 직접 붙여넣을 때는 아래를 지킵니다.

- 에디터의 **HTML(소스) 모드**에서 붙여넣습니다. 기본(위지윅) 모드에 붙이면 `<h3>`·
  `<hr>`·`<p>` 구분이 뭉개져 한 덩어리 글이 됩니다.
- 소스는 Blogger 글의 본문 HTML 또는 `--dry-run` 출력의 `본문:` 아래 전체를 씁니다.
- 제목은 Blogger와 같게 `YYYY년 MM월 DD일 실시간 인기 검색어 TOP N`으로 맞춥니다.
- 카테고리는 `생활&노하우`, 공개 발행으로 올립니다.
- 붙여넣은 뒤 미리보기에서 소제목·문단 간격·기사 링크(새 창)를 한 번 확인합니다.

## 검색어·기사 품질 규칙
- 구글 트렌드 한국 RSS에는 태국어(`สเปน พบ อังกฤษ`), 러시아어(`испания – англия`),
  인도네시아어(`spanyol vs inggris`) 같은 해외 검색어가 섞여 들어옵니다.
  - 한글(가-힣)이 있는 검색어만 남깁니다.
  - 한글이 없어도 `KBO`, `F1`처럼 영문·숫자로만 된 검색어는 국내 기사가 붙어 있을 때만 남깁니다.
    (해외 기사만 붙은 영문 검색어는 해외 스포츠 스팸으로 보고 제외)
  - 태국어·키릴·아랍어·한자·가나 등 비라틴 문자 검색어는 제외합니다.
- 키워드마다 `<ht:news_item>` 목록 전체를 보고 키워드와 실제로 겹치는 기사를 골라 붙입니다
  (이전에는 각 항목의 마지막 기사가 무조건 붙어서 '집'에 배종옥 기사가 붙는 식으로 어긋났습니다).
  키워드와 겹치는 기사가 없으면 기사를 생략합니다.
- 제외된 검색어는 실행 로그에 `- 제외: '...' (사유)` 형태로 남습니다. 그래서 하루 키워드 수는
  10개보다 적을 수 있고, 제목의 `TOP N`도 실제 개수로 표시됩니다(트렌드 RSS는 최대 10개만 제공).

## 같은 날 중복 발행 방지
- 실제 발행(`DRY_RUN=false`) 시 Blogger API로 최근 글을 확인해, 제목이 오늘 날짜(KST)
  (`YYYY년 MM월 DD일 ...`)로 시작하는 글이 이미 있으면 발행하지 않고 건너뜁니다(로그 남기고 정상 종료).
  GitHub Actions의 `schedule`은 수십 분~수 시간 늦게 실행될 수 있어 같은 날 두 번 올라가는 것을 막기 위한 장치입니다.
- 중복 검사 자체가 실패하면 중복 위험을 피하려고 발행하지 않고 워크플로를 실패로 끝냅니다.
  실제로 오늘 글이 없는 것이 확실할 때는 수동 실행에서 `force=true`로 강제 발행할 수 있습니다.
  ```bash
  gh workflow run daily-trend-post.yml -R iconpos-oss/Settings -f dry_run=false -f force=true
  ```

## 주의사항
- Google Trends RSS는 비공식 엔드포인트라 구글 측에서 형식이 바뀔 수 있습니다
- 뉴스 제목/링크만 인용하고 본문 전체를 복사하지 않도록 스크립트가 설계되어 있습니다 (저작권 문제 방지)

## 안전한 테스트 및 배포 가이드

- 로컬에서 RSS가 차단된 경우 샘플 데이터로 테스트:
   ```bash
   USE_SAMPLE_DATA=1 python "Secrets and variables/Actions/test_runner.py"
   ```

- `post_trends.py`는 `--dry-run` 옵션과 `DRY_RUN` 환경변수를 지원합니다. 게시 전 내용 확인을 권장합니다:
   ```bash
   # CLI 옵션 사용
   python "Secrets and variables/Actions/post_trends.py" --dry-run

   # 또는 환경변수로 설정
   DRY_RUN=true python "Secrets and variables/Actions/post_trends.py"
   ```

- `test_runner.py`는 RSS 파싱 결과와 함께 중복 발행 방지 동작(오늘 글 있음→건너뜀, 없음→발행, force→강제 발행)을 가짜 Blogger 서비스로 검사합니다. 실제 발행은 하지 않습니다:
   ```bash
   cd "Secrets and variables/Actions" && python test_runner.py
   ```

- GitHub Actions 수동 실행 시 기본값은 `dry_run=true`로 되어 있으므로 버튼으로 실행해 결과를 확인하세요. 실제 게시를 원할 경우 수동 실행에서 `dry_run`을 `false`로 설정하거나, 스케줄러(매일 실행)는 자동으로 게시합니다.
