import os
import time
import tempfile
import requests
from urllib.parse import urlparse
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

MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "10"))

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


def download_from_s3(image_path: str) -> str:
    suffix = os.path.splitext(image_path)[1] or ".jpg"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    s3.download_file(S3_BUCKET, image_path, tmp_path)
    return tmp_path


def extract_username_from_url(source_url: str) -> str:
    """https://x.com/username/status/123 → username"""
    try:
        path = urlparse(source_url).path.strip("/")
        parts = path.split("/")
        if len(parts) >= 1 and parts[0] not in ("i", "status"):
            return parts[0]
    except Exception:
        pass
    return "unknown"


def post_image_to_x(image_path_local: str, text: str, username: str) -> bool:
    auth = tweepy.OAuth1UserHandler(
        X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET
    )
    api_v1 = tweepy.API(auth)
    client = tweepy.Client(
        consumer_key=X_API_KEY,
        consumer_secret=X_API_SECRET,
        access_token=X_ACCESS_TOKEN,
        access_token_secret=X_ACCESS_TOKEN_SECRET,
    )

    media = api_v1.media_upload(filename=image_path_local)
    media_id = media.media_id_string

    # 원작자 멘션 추가
    final_text = (text or "").strip()
    if username and username != "unknown":
        mention = f"\n\nvia @{username}"
        # 280자 제한 고려
        if len(final_text) + len(mention) > 280:
            final_text = final_text[: 280 - len(mention) - 3] + "..."
        final_text += mention
    else:
        final_text = final_text or " "

    client.create_tweet(text=final_text[:280], media_ids=[media_id])
    return True


def main():
    print("pending 게시글 조회 중...")
    posts = list_pending_posts()
    print(f"대상 {len(posts)}개")

    success = 0
    for post in posts[:MAX_POSTS_PER_RUN]:
        post_id = post["id"]
        image_path = post["imagePath"]
        content = post.get("content") or ""
        source_url = post.get("sourceUrl") or ""
        username = extract_username_from_url(source_url)

        tmp = None
        try:
            print(f"처리 중: {post_id} / @{username}")
            tmp = download_from_s3(image_path)
            post_image_to_x(tmp, content, username)
            mark_posted(post_id)
            success += 1
            print(f"  성공 → status=posted (via @{username})")
            time.sleep(5)  # rate limit 여유
        except Exception as e:
            print(f"  실패: {e}")
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)

    print(f"완료: {success}개 X 업로드")


if __name__ == "__main__":
    main()
