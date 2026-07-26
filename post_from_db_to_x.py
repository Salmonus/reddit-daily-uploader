import os
import time
import requests
import re
import boto3
import tweepy

GRAPHQL_ENDPOINT = os.environ["GRAPHQL_ENDPOINT"]
API_KEY = os.environ["API_KEY"]
S3_BUCKET = os.environ["S3_BUCKET"]
AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")

X_API_KEY = os.environ["X_API_KEY"]
X_API_SECRET = os.environ["X_API_SECRET"]
X_ACCESS_TOKEN = os.environ["X_ACCESS_TOKEN"]
X_ACCESS_TOKEN_SECRET = os.environ["X_ACCESS_TOKEN_SECRET"]

MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "5"))

s3 = boto3.client("s3", region_name=AWS_REGION)


def gql(query, variables=None):
    headers = {"Content-Type": "application/json", "x-api-key": API_KEY}
    resp = requests.post(
        GRAPHQL_ENDPOINT,
        json={"query": query, "variables": variables or {}},
        headers=headers,
        timeout=30,
    )
    data = resp.json()
    if "errors" in data:
        print("GraphQL errors:", data["errors"])
    return data


def list_pending_posts():
    query = """
    query ListPosts {
      listPosts(limit: 200) {
        items {
          id
          title
          content
          imagePath
          status
          sourceUrl
          redditId
        }
      }
    }
    """
    result = gql(query)
    items = result.get("data", {}).get("listPosts", {}).get("items", []) or []
    return [p for p in items if (p.get("status") or "") == "pending" and p.get("imagePath")]


def mark_posted(post_id: str):
    mutation = """
    mutation UpdatePost($input: UpdatePostInput!) {
      updatePost(input: $input) {
        id
        status
      }
    }
    """
    return gql(mutation, {"input": {"id": post_id, "status": "posted"}})


def has_link(text: str) -> bool:
    """본문에 t.co 또는 x.com / twitter.com 링크가 있는지 확인"""
    if not text:
        return False
    pattern = r'(https?://)?(www\.)?(t\.co|x\.com|twitter\.com)/[^\s]+'
    return bool(re.search(pattern, text, re.IGNORECASE))


def post_text_only_to_x(final_text: str) -> bool:
    """이미지 없이 텍스트만 포스팅"""
    client = tweepy.Client(
        consumer_key=X_API_KEY,
        consumer_secret=X_API_SECRET,
        access_token=X_ACCESS_TOKEN,
        access_token_secret=X_ACCESS_TOKEN_SECRET,
    )

    tweet_text = final_text.strip() if final_text.strip() else " "
    client.create_tweet(text=tweet_text[:280])
    return True


def main():
    print("pending 게시글 조회 중...")
    posts = list_pending_posts()
    print(f"대상 {len(posts)}개")

    success = 0
    skipped_no_link = 0

    for post in posts[:MAX_POSTS_PER_RUN]:
        post_id = post["id"]
        content = post.get("content") or ""
        title = post.get("title") or ""

        # ===== t.co / x.com 링크가 있는 경우만 포스팅 =====
        if not has_link(content):
            skipped_no_link += 1
            print(f"[스킵] t.co/x.com 링크 없음: {post_id}")
            continue

        # ===== 텍스트 + via 구성 =====
        if title.startswith("@"):
            username = title[1:].strip()
            final_text = content.strip()
            if final_text:
                mention = f"\n\nvia @{username}"
                if len(final_text) + len(mention) > 280:
                    final_text = final_text[: 280 - len(mention) - 3] + "..."
                final_text += mention
            else:
                final_text = f"via @{username}"
            print(f"처리 중: {post_id} / via @{username} (이미지 없이 텍스트만)")
        else:
            final_text = content.strip() or " "
            print(f"처리 중: {post_id} / username 없음 → 텍스트만")

        try:
            # 이미지 첨부하지 않고 텍스트만 포스팅
            post_text_only_to_x(final_text)
            mark_posted(post_id)
            success += 1
            print(f"  성공 → status=posted (이미지 없음) | 예상 비용: $0.20")
            time.sleep(8)
        except Exception as e:
            print(f"  실패: {e}")

    print(f"\n완료: {success}개 포스팅 / 링크 없어서 스킵: {skipped_no_link}개")
    print(f"참고: 링크 포함 게시물 1개당 약 $0.20 크레딧이 차감됩니다.")


if __name__ == "__main__":
    main()
