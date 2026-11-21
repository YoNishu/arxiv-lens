import logging

from fastmcp import FastMCP

from app.db import crud
from app.insights import store

logger = logging.getLogger(__name__)

mcp = FastMCP("ArxivLens Assistant")

NO_INSIGHTS = "No insights have been built yet. Run: python -m app insights build"


@mcp.tool()
async def get_daily_digest() -> str:
    """Get today's paper digest — papers from the last 24 hours ranked by novelty score."""
    logger.info("MCP tool: get_daily_digest")
    papers = await crud.get_daily_digest()
    logger.info("MCP get_daily_digest: returning %d papers", len(papers))
    if not papers:
        return "No new papers in the last 24 hours."
    lines = []
    for p in papers:
        a = p.analysis
        lines.append(
            f"**{p.title}** (Score: {a.novelty_score}/10)\n"
            f"Authors: {', '.join(p.authors[:3])}\n"
            f"Summary: {a.summary[:300]}\n"
            f"Keywords: {', '.join(a.keywords)}"
        )
    return "\n\n---\n\n".join(lines)


@mcp.tool()
async def search_papers(query: str) -> str:
    """Search papers by keyword across titles, abstracts, summaries, and keywords."""
    logger.info("MCP tool: search_papers query='%s'", query)
    papers = await crud.search_papers(query)
    logger.info("MCP search_papers: %d results for '%s'", len(papers), query)
    if not papers:
        return f"No papers found matching '{query}'."
    lines = []
    for p in papers[:10]:
        a = p.analysis
        lines.append(
            f"[ID:{p.id}] **{p.title}** — Score: {a.novelty_score}/10\n"
            f"Summary: {a.summary[:200]}"
        )
    return "\n\n".join(lines)


@mcp.tool()
async def get_paper_analysis(paper_id: int) -> str:
    """Get the full analysis for a specific paper."""
    logger.info("MCP tool: get_paper_analysis paper_id=%d", paper_id)
    paper = await crud.get_paper(paper_id)
    if not paper or not paper.analysis:
        return f"Paper {paper_id} not found or not yet analysed."
    a = paper.analysis
    return (
        f"**{paper.title}**\n"
        f"Authors: {', '.join(paper.authors)}\n"
        f"Novelty Score: {a.novelty_score}/10\n\n"
        f"**Summary**: {a.summary}\n\n"
        f"**Key Contributions**:\n" + "\n".join(f"- {c}" for c in a.key_contributions) + "\n\n"
        f"**Methodology**: {a.methodology}\n\n"
        f"**Limitations**: {a.limitations or 'Not specified'}\n\n"
        f"**Abstract**: {paper.abstract[:2000]}"
    )


@mcp.tool()
async def add_category(arxiv_code: str) -> str:
    """Subscribe to a new arXiv category."""
    logger.info("MCP tool: add_category code='%s'", arxiv_code)
    existing = await crud.get_category_by_code(arxiv_code)
    if existing:
        return f"Already subscribed to {arxiv_code}."
    await crud.create_category(name=arxiv_code, arxiv_code=arxiv_code)
    return f"Subscribed to {arxiv_code}. Papers will appear after the next poll."


@mcp.tool()
async def list_categories() -> str:
    """List all subscribed arXiv categories."""
    logger.info("MCP tool: list_categories")
    cats = await crud.get_all_categories()
    if not cats:
        return "No categories subscribed."
    return "\n".join(f"- {c.name} ({c.arxiv_code})" for c in cats)


@mcp.tool()
async def related_papers(arxiv_id: str) -> str:
    """Get similar, cited and citing papers for an arXiv id."""
    logger.info("MCP tool: related_papers arxiv_id='%s'", arxiv_id)
    try:
        related = store.related(arxiv_id)
    except store.NoResult:
        return NO_INSIGHTS
    if related is None:
        return f"Paper {arxiv_id} is not in the insights."
    lines = [
        f"# {related['title']}",
        f"arXiv:{arxiv_id} | {related['state']} | feeds: {', '.join(related['feeds'])}",
        "",
        "## Similar",
    ]
    lines += [f"- {s['arxiv_id']} {s['title']} ({s['score']:.3f})" for s in related["similar"]]
    lines.append("## Cites")
    lines += [f"- {c['arxiv_id']} {c['title']}" for c in related["cites"]]
    lines.append(f"## Cited by ({related['cited_by_count']})")
    lines += [f"- {c['arxiv_id']} {c['title']}" for c in related["cited_by"]]
    return "\n".join(lines)


@mcp.tool()
async def topics() -> str:
    """List the research topics found in the library."""
    logger.info("MCP tool: topics")
    try:
        found = store.topics()
    except store.NoResult:
        return NO_INSIGHTS
    if not found:
        return "No topics yet."
    return "\n".join(
        f"- {t['topic']}: {t['name']} ({t['size']} papers; {', '.join(t['terms'][:3])})" for t in found
    )


@mcp.tool()
async def trends(feed: str) -> str:
    """Get the rising terms of a subscribed category."""
    logger.info("MCP tool: trends feed='%s'", feed)
    try:
        found = store.trends(feed)
    except store.NoResult:
        return NO_INSIGHTS
    if found is None:
        return f"No trends for {feed}."
    if not found["rising"]:
        return f"Nothing is rising in {feed}."
    lines = [f"Rising in {feed}:"]
    lines += [f"**{r['term']}** {r['recent']}/{r['baseline']} z={r['z']:.2f}" for r in found["rising"]]
    return "\n".join(lines)
