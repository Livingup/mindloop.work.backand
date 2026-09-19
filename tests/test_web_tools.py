import pytest

from agent_runtime.web_tools.adapters import (
    build_read_url_user_prompt,
    build_web_search_user_prompt,
    build_web_tool_system_prompt,
)
from agent_runtime.web_tools.models import ReadUrlContextRequest, WebSearchRequest
from agent_runtime.web_tools.service import (
    _clean_trailing_url_punct,
    _enrich_sources,
    _extract_markdown_images,
    _extract_raw_sources_and_images,
    _is_image_url,
    _normalize_image_url,
    _parse_result_data,
    _render_html_document,
    _render_images,
)


def test_build_web_search_user_prompt():
    req1 = WebSearchRequest(query="Donald Trump official biography")
    assert build_web_search_user_prompt(req1) == "Donald Trump official biography"

    req2 = WebSearchRequest(
        query="Donald Trump portrait",
        allowed_domains=("commons.wikimedia.org", "whitehouse.gov"),
        blocked_domains=("bad.com",),
        recency="past_month",
    )
    prompt = build_web_search_user_prompt(req2)
    assert "Donald Trump portrait" in prompt
    assert "Allowed domains: commons.wikimedia.org, whitehouse.gov" in prompt
    assert "Blocked domains: bad.com" in prompt
    assert "Recency: past_month" in prompt


def test_build_read_url_user_prompt():
    req1 = ReadUrlContextRequest(url="https://example.com/page")
    assert "https://example.com/page" in build_read_url_user_prompt(req1)

    req2 = ReadUrlContextRequest(url="https://example.com/page", question="What is the release date?")
    prompt = build_read_url_user_prompt(req2)
    assert "https://example.com/page" in prompt
    assert "What is the release date?" in prompt


def test_is_image_url():
    assert _is_image_url("https://example.com/photo.jpg") is True
    assert _is_image_url("https://example.com/photo.PNG?w=800") is True
    assert _is_image_url("https://example.com/photo.webp#anchor") is True
    assert _is_image_url("https://upload.wikimedia.org/wikipedia/commons/1/16/Trump.jpg") is True
    assert _is_image_url("https://example.com/article.html") is False
    assert _is_image_url("") is False


def test_clean_trailing_url_punct():
    assert _clean_trailing_url_punct("https://example.com/image.jpg.") == "https://example.com/image.jpg"
    assert _clean_trailing_url_punct("https://example.com/image.jpg,") == "https://example.com/image.jpg"
    # Balanced parentheses preserved
    assert _clean_trailing_url_punct("https://example.com/Trump_(2025).jpg") == "https://example.com/Trump_(2025).jpg"
    # Unbalanced closing parenthesis stripped
    assert _clean_trailing_url_punct("https://example.com/image.jpg)") == "https://example.com/image.jpg"


def test_extract_raw_sources_and_images_from_responses_api():
    raw_response = {
        "output": [
            {
                "type": "web_search_call",
                "action": {
                    "type": "search",
                    "query": "Donald Trump portrait",
                },
            },
            {
                "type": "web_search_call",
                "action": {
                    "type": "open_page",
                    "url": "https://whitehouse.gov/trump.png",
                },
            },
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Found portrait.",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "title": "White House Portrait.png",
                                "url": "https://whitehouse.gov/portrait.png",
                            },
                            {
                                "type": "url_citation",
                                "title": "White House Bio",
                                "url": "https://whitehouse.gov/bio",
                            },
                        ],
                    }
                ],
            },
        ]
    }
    sources, images = _extract_raw_sources_and_images(raw_response)
    assert len(sources) == 3
    source_urls = {s["url"] for s in sources}
    assert "https://whitehouse.gov/trump.png" in source_urls
    assert "https://whitehouse.gov/portrait.png" in source_urls
    assert "https://whitehouse.gov/bio" in source_urls

    assert len(images) == 2
    image_urls = {img["url"] for img in images}
    assert "https://whitehouse.gov/trump.png" in image_urls
    assert "https://whitehouse.gov/portrait.png" in image_urls


def test_extract_markdown_images_and_links():
    text = (
        "Here are the images:\n"
        "![Markdown Image](https://example.com/img1.png)\n"
        "Check [Link Image](https://example.com/img2.jpg) for details.\n"
        "Direct link: https://example.com/img3.webp\n"
        "Normal link: [Webpage](https://example.com/page.html)\n"
    )
    images = _extract_markdown_images(text)
    assert len(images) == 3
    image_urls = [img["url"] for img in images]
    assert "https://example.com/img1.png" in image_urls
    assert "https://example.com/img2.jpg" in image_urls
    assert "https://example.com/img3.webp" in image_urls


def test_enrich_sources_and_render_html():
    raw_response = {
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "title": "Official Bio",
                                "url": "https://whitehouse.gov/bio",
                            }
                        ],
                    }
                ],
            }
        ]
    }
    raw_content = (
        "President biography text.\n\n"
        "- High-res photo: https://upload.wikimedia.org/photo.jpg\n"
        "- Source: [White House](https://whitehouse.gov/bio)\n"
    )
    data = _parse_result_data(raw_content)
    _enrich_sources(data, raw_response, raw_content=raw_content)

    assert len(data["images"]) >= 1
    assert data["images"][0]["url"] == "https://upload.wikimedia.org/photo.jpg"
    assert len(data["citations"]) >= 1

    req = WebSearchRequest(query="Donald Trump")
    html_content = _render_html_document("web_search", req, data)
    assert "<article>" in html_content
    assert "<h2>Summary</h2>" in html_content
    assert "<h2>Images</h2>" in html_content
    assert '<img src="https://upload.wikimedia.org/photo.jpg"' in html_content
    assert 'referrerpolicy="no-referrer"' in html_content
    assert "<h2>Sources</h2>" in html_content


def test_normalize_image_url():
    wiki_commons = "https://commons.wikimedia.org/wiki/File:Donald_Trump_official_portrait.jpg"
    assert _normalize_image_url(wiki_commons) == "https://commons.wikimedia.org/wiki/Special:FilePath/Donald_Trump_official_portrait.jpg"

    wikipedia = "https://en.wikipedia.org/wiki/File:Example_Photo.png?lang=en"
    assert _normalize_image_url(wikipedia) == "https://en.wikipedia.org/wiki/Special:FilePath/Example_Photo.png"

    normal_img = "https://example.com/photo.jpg"
    assert _normalize_image_url(normal_img) == "https://example.com/photo.jpg"


def test_render_images_includes_no_referrer():
    images = [
        {
            "url": "https://commons.wikimedia.org/wiki/File:Test_Portrait.jpg",
            "title": "Test Portrait",
            "source_url": "https://commons.wikimedia.org/wiki/File:Test_Portrait.jpg",
        }
    ]
    html_output = _render_images(images)
    assert 'src="https://commons.wikimedia.org/wiki/Special:FilePath/Test_Portrait.jpg"' in html_output
    assert 'referrerpolicy="no-referrer"' in html_output
    assert 'alt="Test Portrait"' in html_output


def test_render_html_document_batch_groups_query_and_results():
    batch_data = {
        "items": [
            {
                "target": "Donald Trump official biography",
                "success": True,
                "summary": "Donald Trump was born in New York...",
                "images": [],
                "key_points": ["45th and 47th President"],
            },
            {
                "target": "Donald Trump official portrait",
                "success": True,
                "summary": "Official portrait from the White House.",
                "images": [
                    {
                        "url": "https://commons.wikimedia.org/wiki/File:Official_portrait.jpg",
                        "title": "White House Portrait",
                    }
                ],
                "key_points": [],
            },
        ],
        "batch": {
            "requested_count": 2,
            "success_count": 2,
        },
        "results": [
            {"title": "White House Bio", "url": "https://whitehouse.gov/bio"},
        ],
        "citations": [
            {"title": "White House Bio", "url": "https://whitehouse.gov/bio"},
        ],
    }
    req = WebSearchRequest(query="Donald Trump official biography", additional_queries=("Donald Trump official portrait",))
    html_content = _render_html_document("web_search", req, batch_data)

    # 1. Header has summary count, not detached query list
    assert "Completed 2/2 queries" in html_content
    assert "<dt>Query</dt>" not in html_content

    # 2. Item 1 has its query as header and its summary text right below
    assert "<h2>1. Donald Trump official biography</h2>" in html_content
    assert "<p style=\"white-space: pre-wrap;\">Donald Trump was born in New York...</p>" in html_content
    assert "45th and 47th President" in html_content

    # 3. Item 2 has its query as header, its summary text, and its portrait image right below
    assert "<h2>2. Donald Trump official portrait</h2>" in html_content
    assert "Official portrait from the White House." in html_content
    assert '<img src="https://commons.wikimedia.org/wiki/Special:FilePath/Official_portrait.jpg"' in html_content
    assert 'referrerpolicy="no-referrer"' in html_content

    # 4. Global sources in footer
    assert "<footer>" in html_content
    assert "<h2>Sources</h2>" in html_content
    assert "https://whitehouse.gov/bio" in html_content

