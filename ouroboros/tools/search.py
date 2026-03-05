"""Web search tool."""

from __future__ import annotations

import json
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry


def _web_search(ctx: ToolContext, query: str) -> str:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        return json.dumps(
            {"error": "duckduckgo_search package not installed. Run `pip install duckduckgo-search`."},
            ensure_ascii=False
        )

    try:
        with DDGS() as ddgs:
            # Fetch the top 5 results for the query
            results = list(ddgs.text(query, max_results=5))
            
        if not results:
            return json.dumps({"answer": "(no results found)"}, ensure_ascii=False, indent=2)
        
        # Format the results into a clear, readable text block
        formatted_snippets = []
        for result in results:
            title = result.get("title", "No Title")
            body = result.get("body", "No Description")
            link = result.get("href", "No Link")
            formatted_snippets.append(f"Title: {title}\nSnippet: {body}\nLink: {link}")
            
        answer_text = "\n\n".join(formatted_snippets)
        
        return json.dumps(
            {
                "answer": answer_text,
                "sources": results
            }, 
            ensure_ascii=False, 
            indent=2
        )
    except Exception as e:
        return json.dumps({"error": repr(e)}, ensure_ascii=False)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("web_search", {
            "name": "web_search",
            "description": "Search the web via DuckDuckGo. Returns JSON with answer snippets and source links.",
            "parameters": {
                "type": "object", 
                "properties": {
                    "query": {"type": "string"},
                }, 
                "required": ["query"]
            },
        }, _web_search),
    ]