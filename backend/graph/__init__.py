"""
Graph Module
"""
from .state import GlobalState

__all__ = [
    "GlobalState",
]


def __getattr__(name):
    if name in ["travel_graph", "create_travel_planning_graph", "create_intra_city_subgraph"]:
        from .workflow import travel_graph, create_travel_planning_graph, create_intra_city_subgraph
        return locals()[name]
    raise AttributeError(f"module {__name__} has no attribute {name}")