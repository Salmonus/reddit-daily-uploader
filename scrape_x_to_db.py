import os
import time
import requests
from datetime import datetime, timezone
from urllib.parse import urlparse
from apify_client import ApifyClient
import boto3

# ===== 환경변수 =====
APIFY_TOKEN = os.environ["APIFY_TOKEN"]
SEARCH_KEYWORDS = [k.strip() for k in os.environ.get("SEARCH_KEYWORD", "AI").split(",") if k.strip()]
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "50"))
LIMIT_UPLOAD = int(os.environ.get("LIMIT_UPLOAD", "20"))
S3_BUCKET = os.environ["S3_BUCKET"]
AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
GRAPHQL_ENDPOINT = os.environ["GRAPHQL_ENDPOINT"]
API_KEY = os.environ["API_KEY"]

s3 = boto3.client("s3", region_name=AWS_REGION)


def is_image_url(url: str) -> bool:
    """이미지 URL인지 판단"""
    if not isinstance(url, str):
        return False
    url_lower = url.lower()
    if "pbs.twimg.com" not in url_lower and "twimg.com" not in url_lower:
        return False
    # 비디오 썸네일 제외
    if "video_thumb" in url_lower or "amplify_video_thumb" in url_lower:
        return False
    if any(ext in url_lower for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]):
        return True
    if "name=" in url_lower or "/media/" in url_lower:
        return True
    return True


def find_image_urls(obj, found=None) -> list[str]:
    """최대한 강력하게 이미지 URL을 찾는 함수"""
    if found is None:
        found = []

    if isinstance(obj, dict):
        for key in [
            "media_url_https", "media_url", "url", "mediaUrl", "image", "photo",
            "src", "href", "expanded_url", "display_url", "coverImage"
        ]:
            val = obj.get(key)
            if is_image_url(val):
                found.append(val)

        for key in ["media", "photos", "images", "extended_entities", "entities"]:
            val = obj.get(key)
            if isinstance(val, list):
                for item in val:
                    find_image_urls(item, found)
            elif isinstance(val, dict):
                find_image_urls(val, found)

        for key, val in obj.items():
            if key in ["media_url_https", "media_url", "url", "media", "photos", "images"]:
                continue
            find_image_urls(val, found)

    elif isinstance(obj, list):
        for item in obj:
            find_image_urls(item, found)

    elif isinstance(obj, str):
        if is_image_url(obj):
            found.append(obj)

    cleaned = []
    seen = set()
    for u in found:
        u = u.replace("&amp;", "&").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        if "name=" not in u and "pbs.twimg.com/media/" in u:
            u = u + ("&name=large" if "?" in u else "?name=large")
        cleaned.append(u)

    cleaned.sort(key=lambda x: ("video_thumb" in x.lower(), x))
    return cleaned


def is_original_post(item: dict) -> bool:
    """reply / retweet이면 False, 원본이면 True"""
    # 여러 가지 필드명 대응
    if item.get("isReply") or item.get("is_reply") or item.get("inReplyToId") or item.get("in_reply_to_status_id"):
        return False
    if item.get("isRetweet") or item.get("is_retweet") or item.get("retweeted") or item.get("retweeted_status"):
        return False
    if item.get("isQuote") and item.get("quotedStatus"):  # 인용 트윗도 원본이 아닐 수 있음 (선택)
        # 인용은 허용하려면 이 부분 주석 처리
        pass
    return True


def get_like_count(item: dict) -> int:
    """좋아요 수 추출"""
    return (
        item.get("likeCount")
        or item.get("favorite_count")
        or item.get("favorites")
        or item.get("likes")
        or item.get("favoriteCount")
        or 0
    )


def upload_to_s3(image_bytes: bytes, key: str, content_type: str = "image/jpeg"):
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=image_bytes, ContentType=content_type)
    return key


def create_post(title, content, image_path, source_url=None, tweet_id=None):
    mutation = """
    mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        id
        title
        imagePath
        redditId
        status
        fetchedAt
      }
    }
    """
    variables = {
        "input": {
            "title": (title or "Untitled")[:200],
            "content": content or "",
            "imagePath": image_path,
            "sourceUrl": source_url,
            "redditId": tweet_id,
            "status": "pending",
            "fetchedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    }
    headers = {"Content-Type": "application/json", "x-api-key": API_KEY}
    resp = requests.post(GRAPHQL_ENDPOINT, json={"query": mutation, "variables": variables}, headers=headers, timeout=30)
    result = resp.json()
    print("GraphQL status:", resp.status_code)
    print("GraphQL 응답:", result)
    return result


def already_exists(tweet_id: str) -> bool:
    query = """
    query ListPosts {
      listPosts(limit: 500) {
        items {
          redditId
        }
      }
    }
    """
    headers = {"Content-Type": "application/json", "x-api-key": API_KEY}
    try:
        resp = requests.post(GRAPHQL_ENDPOINT, json={"query": query}, headers=headers, timeout=20)
        result = resp.json()
        items = result.get("data", {}).get("listPosts", {}).get("items", []) or []
        return any(item.get("redditId") == tweet_id for item in items)
    except Exception as e:
        print(f"already_exists 체크 실패: {e}")
        return False


def main():
    print(f"===== X 스크래핑 시작 ({datetime.now(timezone.utc)}) =====")
    print(f"키워드: {SEARCH_KEYWORDS} / 저장 목표: {LIMIT_UPLOAD}개")
    print("필터: 원본 게시물만 + 좋아요 100개 이상")

    client = ApifyClient(APIFY_TOKEN)

    run_input = {
        "searchTerms": SEARCH_KEYWORDS,
        "sort": "Top",
        "maxItems": MAX_ITEMS,
    }

    print("[Apify] Top 포스트 수집 중...")
    run = client.actor("apidojo/tweet-scraper").call(run_input=run_input)

    dataset_id = (
        getattr(run, "default_dataset_id", None)
        or getattr(run, "defaultDatasetId", None)
    )

    if not dataset_id:
        print("데이터셋 ID 없음")
        print("run 객체:", run)
        return

    items = list(client.dataset(dataset_id).iterate_items())
    print(f"수집된 포스트: {len(items)}개")

    # 좋아요 많은 순으로 정렬
    items.sort(key=lambda x: get_like_count(x), reverse=True)

    success_count = 0
    no_image_count = 0
    skipped_reply_retweet = 0
    skipped_low_likes = 0

    for item in items:
        if success_count >= LIMIT_UPLOAD:
            break

        tweet_id = str(item.get("id") or item.get("tweetId") or "")
        if not tweet_id:
            continue

        # ===== 1. 원본 게시물인지 체크 =====
        if not is_original_post(item):
            skipped_reply_retweet += 1
            print(f"[스킵] reply/repost: {tweet_id}")
            continue

        # ===== 2. 좋아요 100개 이상인지 체크 =====
        like_count = get_like_count(item)
        if like_count < 100:
            skipped_low_likes += 1
            print(f"[스킵] 좋아요 부족 ({like_count}): {tweet_id}")
            continue

        if already_exists(tweet_id):
            print(f"[스킵] 이미 존재: {tweet_id}")
            continue

        image_urls = find_image_urls(item)

        if not image_urls:
            no_image_count += 1
            print(f"[스킵] 이미지 없음: {tweet_id}")
            continue

        # 원작자 username 추출
        author = item.get("author") or {}
        username = (
            author.get("userName")
            or author.get("username")
            or author.get("screen_name")
            or author.get("screenName")
            or None
        )

        valid_image_url = image_urls[0]
        text = item.get("text") or item.get("full_text") or ""
        source_url = item.get("url") or item.get("twitterUrl") or f"https://x.com/i/status/{tweet_id}"

        if username:
            title = f"@{username}"
        else:
            title = text[:200] if text else f"X post {tweet_id}"

        try:
            response = requests.get(valid_image_url, timeout=25, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            if response.status_code != 200:
                print(f"이미지 다운로드 실패 ({response.status_code}): {valid_image_url[:80]}")
                continue

            path = urlparse(valid_image_url).path
            ext = os.path.splitext(path)[1].lower()
            if ext not in [".jpg", ".jpeg", ".png", ".gif", ".webp"]:
                ext = ".jpg"

            s3_key = f"images/x_{tweet_id}{ext}"
            content_type = response.headers.get("Content-Type", "image/jpeg")

            upload_to_s3(response.content, s3_key, content_type)
            print(f"S3 업로드 완료: {s3_key} (title: {title} | likes: {like_count})")

            result = create_post(title, text, s3_key, source_url, tweet_id)

            post_id_created = result.get("data", {}).get("createPost", {}).get("id")
            if post_id_created:
                success_count += 1
                print(f"  [{success_count}] DB 저장 성공: {title}")
            else:
                print(f"  DB 저장 실패: {result}")

            time.sleep(0.4)

        except Exception as e:
            print(f"  에러 {tweet_id}: {e}")

    print(f"\n===== 스크래핑 완료! =====")
    print(f"성공 저장: {success_count}개")
    print(f"스킵 - reply/repost: {skipped_reply_retweet}개")
    print(f"스킵 - 좋아요 부족: {skipped_low_likes}개")
    print(f"스킵 - 이미지 없음: {no_image_count}개")


if __name__ == "__main__":
    main()
