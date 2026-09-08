"""
Tests for LangGraph compilation and topology.
"""
import pytest
from app.agent.graph import build_review_graph, review_graph


def test_graph_compilation():
    """Graph compiles successfully without exceptions."""
    compiled = build_review_graph()
    assert compiled is not None


def test_graph_contains_expected_nodes():
    """Graph contains retriever, analyzer, validator, investigator, aggregator, and poster nodes."""
    compiled = review_graph
    nodes = compiled.nodes
    expected_nodes = {"retriever", "analyzer", "validator", "investigator", "aggregator", "poster"}
    for node_name in expected_nodes:
        assert node_name in nodes, f"Expected node '{node_name}' not found in graph nodes"
