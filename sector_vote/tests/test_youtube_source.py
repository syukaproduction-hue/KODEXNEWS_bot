from sector_vote.youtube_source import parse_feed

FEED = '''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <entry>
    <yt:videoId>one</yt:videoId><title>박종훈팀장 바이킹스 오늘 전망</title>
    <published>2026-08-24T00:10:00+00:00</published>
  </entry>
  <entry>
    <yt:videoId>two</yt:videoId><title>다른 출연자의 경제 이야기</title>
    <published>2026-08-24T00:20:00+00:00</published>
  </entry>
</feed>'''


def test_parse_feed_applies_optional_title_filter():
    videos = parse_feed(FEED, title_keywords=["박종훈", "바이킹스"])

    assert videos == [{
        "video_id": "one",
        "title": "박종훈팀장 바이킹스 오늘 전망",
        "published_at": "2026-08-24T00:10:00+00:00",
        "url": "https://www.youtube.com/watch?v=one",
    }]
