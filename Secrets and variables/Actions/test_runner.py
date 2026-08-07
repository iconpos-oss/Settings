"""
로컬/CI에서 안전하게 실행 가능한 간단한 검사 스크립트입니다.
이 스크립트는 `post_trends.py`의 RSS 파싱과 포스트 생성 로직만 실행하고
Blogger API 호출(발행)은 수행하지 않습니다.
"""
from post_trends import fetch_trends, build_post
import os


def main():
    items = fetch_trends()
    # 개발/CI 환경에서 RSS가 차단될 경우, 샘플 데이터로 대체할 수 있게 함
    if not items and os.environ.get("USE_SAMPLE_DATA", "0") in ("1", "true", "True"):
        items = [
            ("샘플키워드1", "샘플 뉴스 제목1", "https://example.com/1"),
            ("샘플키워드2", "샘플 뉴스 제목2", "https://example.com/2"),
            ("샘플키워드3", "샘플 뉴스 제목3", "https://example.com/3"),
        ]

    print(f"가져온 트렌드 개수: {len(items)}")
    if not items:
        print("트렌드 항목이 없습니다.")
        return

    # 샘플로 상위 5개만 출력
    for i, (kw, news_title, news_url) in enumerate(items[:5], start=1):
        print(f"{i}. {kw} | {news_title} | {news_url}")

    title, body = build_post(items[:5])
    print("\n생성된 포스트 제목:\n", title)
    print("\n생성된 포스트 본문(일부):\n", body[:500])


if __name__ == '__main__':
    main()
