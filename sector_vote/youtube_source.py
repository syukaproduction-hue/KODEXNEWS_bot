"""YouTube RSS discovery and transcript retrieval."""

import xml.etree.ElementTree as ET

import requests

ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"


def parse_feed(xml_text: str, title_keywords: list[str] | None = None) -> list[dict]:
    root = ET.fromstring(xml_text)
    videos = []
    for entry in root.findall(f"{ATOM}entry"):
        video_id = (entry.findtext(f"{YT}videoId") or "").strip()
        title = (entry.findtext(f"{ATOM}title") or "").strip()
        published = (entry.findtext(f"{ATOM}published") or "").strip()
        if not video_id:
            continue
        if title_keywords and not any(keyword.lower() in title.lower() for keyword in title_keywords):
            continue
        videos.append({
            "video_id": video_id,
            "title": title,
            "published_at": published,
            "url": f"https://www.youtube.com/watch?v={video_id}",
        })
    return videos


def fetch_latest_videos(channel: dict, timeout: int = 15) -> list[dict]:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel['channel_id']}"
    response = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    return parse_feed(response.text, channel.get("title_keywords"))


def fetch_transcript(video_id: str) -> str:
    from youtube_transcript_api import YouTubeTranscriptApi

    transcript = YouTubeTranscriptApi().fetch(video_id, languages=["ko", "en"])
    return " ".join(snippet.text.strip() for snippet in transcript if snippet.text.strip())
