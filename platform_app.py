"""Standalone Sixnine entrypoint; independent of the original H3 data directory."""
from studio_platform.api import create_app
from studio_platform.settings import Settings

app = create_app(Settings.from_environment())
