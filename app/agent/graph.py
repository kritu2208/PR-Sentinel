"""
LangGraph StateGraph definition and compilation for PR Sentinel review pipeline.
"""
import logging
from langgraph.graph import END, START, StateGraph

from app.agent.nodes.aggregator import aggregator_node
from app.agent.nodes.analyzer import analyzer_node
from app.agent.nodes.investigator import investigator_node
from app.agent.nodes.poster import poster_node
from app.agent.nodes.retriever import retriever_node
from app.agent.state import ReviewState
from app.agent.validator import validator_node

logger = logging.getLogger("pr-sentinel.graph")


def build_review_graph():
    """
    Constructs and compiles the PR review StateGraph.
    Flow: START -> retriever -> analyzer -> validator -> investigator -> aggregator -> poster -> END
    """
    builder = StateGraph(ReviewState)

    builder.add_node("retriever", retriever_node)
    builder.add_node("analyzer", analyzer_node)
    builder.add_node("validator", validator_node)
    builder.add_node("investigator", investigator_node)
    builder.add_node("aggregator", aggregator_node)
    builder.add_node("poster", poster_node)

    builder.add_edge(START, "retriever")
    builder.add_edge("retriever", "analyzer")
    builder.add_edge("analyzer", "validator")
    builder.add_edge("validator", "investigator")
    builder.add_edge("investigator", "aggregator")
    builder.add_edge("aggregator", "poster")
    builder.add_edge("poster", END)

    logger.info("Compiled PR Sentinel review LangGraph successfully")
    return builder.compile()


review_graph = build_review_graph()
