"""Read-only web panel for the decision trail."""

from .api import PanelData
from .server import PanelConfig, serve

__all__ = ["PanelConfig", "PanelData", "serve"]
