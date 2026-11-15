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
    pt.fetch_trends = lambda limit=pt.MAX_ITEMS: (list(SAMPLE_ITEMS), [])

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


def main():
    check_rss()
    ok = check_duplicate_guard()
    if not ok:
        sys.exit(1)


if __name__ == '__main__':
    main()
