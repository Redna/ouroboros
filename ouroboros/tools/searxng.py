"""Web search tool via local SearXNG."""

from __future__ import annotations

import json
import requests
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry

SEARXNG_URL = "http://searxng:8080/search"

def _searxng_search(ctx: ToolContext | None, query: str) -> str:
    """Search the web via local SearXNG instance."""
    try:
        params = {
            "q": query,
            "format": "json",
            "engines": "google,duckduckgo,brave,bing",
        }
        
        response = requests.get(SEARXNG_URL, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        results = data.get("results", [])
        if not results:
            return json.dumps({"answer": "(no results found)"}, ensure_ascii=False, indent=2)
        
        # Format the top 5 results
        formatted_snippets = []
        sources = []
        for result in results[:5]:
            title = result.get("title", "No Title")
            body = result.get("content", "No Description")
            link = result.get("url", "No Link")
            formatted_snippets.append(f"Title: {title}\nSnippet: {body}\nLink: {link}")
            sources.append({
                "title": title,
                "href": link,
                "body": body
            })
            
        answer_text = "\n\n".join(formatted_snippets)
        
        return json.dumps(
            {
                "answer": answer_text,
                "sources": sources
            }, 
            ensure_ascii=False, 
            indent=2
        )
    except Exception as e:
        return json.dumps({"error": f"SearXNG error: {repr(e)}"}, ensure_ascii=False)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("web_search", {
            "name": "web_search",
            "description": "Search the web via local SearXNG (Google/DDG/Brave). Returns JSON with snippets and links.",
            "parameters": {
                "type": "object", 
                "properties": {
                    "query": {"type": "string"},
                }, 
                "required": ["query"]
            },
        }, _searxng_search),
    ]
