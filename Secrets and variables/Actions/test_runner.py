"""
로컬/CI에서 안전하게 실행 가능한 검사 스크립트입니다.
`post_trends.py`의 RSS 파싱·글 생성 로직과 중복 발행 방지 로직만 실행하고
Blogger API로 실제 글을 쓰지는 않습니다(가짜 서비스로 대체).

    python test_runner.py            # RSS 실제 호출 + 중복 방지 시뮬레이션
    USE_SAMPLE_DATA=1 python test_runner.py   # RSS가 막힌 환경에서는 샘플 데이터 사용
"""
import os
import sys

import post_trends as pt

SAMPLE_ITEMS = [
    {"keyword": "샘플키워드1", "traffic": "500+", "news_title": "샘플 뉴스 제목1", "news_url": "https://example.com/1", "news_source": "샘플일보"},
    {"keyword": "샘플키워드2", "traffic": "200+", "news_title": "샘플 뉴스 제목2", "news_url": "https://example.com/2", "news_source": "샘플일보"},
    {"keyword": "샘플키워드3", "traffic": "100+", "news_title": "샘플 뉴스 제목3", "news_url": "https://example.com/3", "news_source": "샘플일보"},
]


class _FakeRequest:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _FakePosts:
    """Blogger API posts.list / posts.insert 대역. 실제 발행은 하지 않는다."""

    def __init__(self, existing_post):
        self.existing_post = existing_post
        self.inserted = []

    def list(self, **kwargs):
        items = [self.existing_post] if self.existing_post else []
        return _FakeRequest(lambda: {"items": items})

    def insert(self, **kwargs):
        self.inserted.append(kwargs)
        return _FakeRequest(lambda: {"url": "https://example.blogspot.com/fake"})


class _FakeService:
    def __init__(self, existing_post):
        self._posts = _FakePosts(existing_post)

    def posts(self):
        return self._posts


def check_rss():
    items, rejected = pt.fetch_trends()
    if not items and os.environ.get("USE_SAMPLE_DATA", "0") in ("1", "true", "True"):
        print("(RSS를 받지 못해 샘플 데이터로 대체합니다)")
        items, rejected = SAMPLE_ITEMS, []
    if not items:
        print("트렌드 항목이 없습니다.")
        return

    pt.print_summary(items, rejected)
    title, body = pt.build_post(items)
    print("\n생성된 포스트 제목:\n", title)
    print("\n생성된 포스트 본문(일부):\n", body[:800])


def check_duplicate_guard():
    """같은 날짜 글이 이미 있을 때 발행을 건너뛰는지 가짜 서비스로 확인한다."""
    today = pt.datetime.datetime.now(pt.KST).date().strftime(pt.DATE_TITLE_FORMAT)
    existing = {"title": f"{today} 실시간 인기 검색어 TOP 10",
                "url": "https://rnlab.blogspot.com/2026/11/existing.html"}

    original_fetch = pt.fetch_trends
    original_service = pt.get_blogger_service
    pt.fetch_trends = lambda limit=pt.MAX_ITEMS, enrich=None: (list(SAMPLE_ITEMS), [])

    results = []
    cases = [
        ("오늘 글 있음 + force 없음 → 건너뜀", existing, False, 0),
        ("오늘 글 없음 → 발행", None, False, 1),
        ("오늘 글 있음 + force → 강제 발행", existing, True, 1),
    ]
    os.environ["BLOGGER_BLOG_ID"] = "6023330275183611634"
    original_argv = sys.argv
    try:
        for label, post, force, expected_inserts in cases:
            service = _FakeService(post)
            pt.get_blogger_service = lambda service=service: service
            os.environ["DRY_RUN"] = "false"
            os.environ["FORCE_PUBLISH"] = "true" if force else "false"
            sys.argv = ["post_trends.py"]
            print(f"\n[중복 방지 검사] {label}")
            try:
                pt.main()
            except SystemExit as e:
                print(f"  (exit code {e.code})")
            ok = len(service._posts.inserted) == expected_inserts
            results.append(ok)
            print(f"  → insert 호출 {len(service._posts.inserted)}회 (기대 {expected_inserts}회) {'OK' if ok else 'FAIL'}")
    finally:
        pt.fetch_trends = original_fetch
        pt.get_blogger_service = original_service
        sys.argv = original_argv
        os.environ.pop("DRY_RUN", None)
        os.environ.pop("FORCE_PUBLISH", None)

    print("\n중복 방지 검사 결과:", "전체 통과" if all(results) else "실패 있음")
    return all(results)


def check_post_format():
    """글 본문 HTML 구조·이스케이프·링크 검사를 실제 발행 없이 확인한다.\n"""
    items = [
        {
            "keyword": "테스트<키워드",
            "traffic": "500+",
            "news_title": '제목 "인용" <b>강조</b>',
            "news_url": "https://example.com/a?x=1&y=2",
            "news_source": "테스트일보",
            "summary": "첫 번째 문장입니다. 두 번째 문장입니다. 세 번째 문장입니다. 네 번째 문장입니다.",
            "summary_source": "article",
        },
        {
            "keyword": "링크없음",
            "traffic": "",
            "news_title": "링크 없는 기사",
            "news_url": "javascript:alert(1)",
            "news_source": "",
            "summary": "",
            "summary_source": "",
        },
        {
            "keyword": "기사없음",
            "traffic": "100+",
            "news_title": "",
            "news_url": "",
            "news_source": "",
            "summary": "",
            "summary_source": "",
        },
    ]

    title, body = pt.build_post(items)
    today = pt.datetime.datetime.now(pt.KST).date().strftime(pt.DATE_TITLE_FORMAT)

    checks = []
    def check(label, ok):
        checks.append(ok)
        print(f"  {'OK ' if ok else 'FAIL'}  {label}")

    print("\n[글 포맷 검사]")
    check("제목이 오늘(KST) 날짜로 시작", title.startswith(f"{today} 실시간 인기 검색어 TOP 3"))
    check("서론 문단 있음", body.startswith("<p>") and "정리했습니다" in body)
    check("마무리 문단 있음", "자동으로 작성됩니다" in body)
    check("순위별 소제목(<h3>)", body.count("<h3>") == 3 and "<h3>1. 테스트" in body)
    check("검색 관심도 표기", "<strong>검색 관심도</strong>" in body)
    check("요약 3문장 이내", "네 번째 문장" not in body)
    check("기사 없음 안내", "기사 없음" in body and "포털 뉴스에서" in body)
    check("링크 없는 기사 표시", "(원문 링크 제공 없음)" in body)
    check("새 창 링크 속성", 'target="_blank" rel="noopener noreferrer"' in body)
    check("javascript: 링크 버림", "javascript:" not in body)
    check("본문 이스케이프(<script> 없음)", "<script" not in body)
    check("키워드 이스케이프", "테스트&lt;키워드" in body)
    check("속성 URL 이스케이프", "x=1&amp;y=2" in body)
    check("블록마다 줄바꿈 분리", all(
        line.count("<p>") + line.count("<h3>") + line.count("<h2>") <= 1
        for line in body.split("\n")
    ))
    check("태그 짝 맞음", body.count("<p>") == body.count("</p>")
          and body.count("<h3>") == body.count("</h3>")
          and body.count("<ul>") == body.count("</ul>")
          and body.count("<li>") == body.count("</li>"))

    print("\n[본문 미리보기]")
    print(body)

    print("\n글 포맷 검사 결과:", "전체 통과" if all(checks) else "실패 있음")
    return all(checks)


def main():
    check_rss()
    ok = check_duplicate_guard()
    ok = check_post_format() and ok
    if not ok:
        sys.exit(1)


if __name__ == '__main__':
    main()
