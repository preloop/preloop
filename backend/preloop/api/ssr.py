import os
import logging
from pathlib import Path
import httpx

logger = logging.getLogger(__name__)


class SSRService:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.index_template_path = self.base_dir / "frontend" / "dist" / "index.html"
        self.render_server_url = os.getenv(
            "SSR_RENDER_SERVER_URL", "http://localhost:3001/render"
        )

    def render_app(self, url: str) -> str:
        """
        Renders the Lit application by calling the external Node.js render server.
        """
        if not self.index_template_path.exists():
            logger.error(
                f"Index template not found at {self.index_template_path}. Please run `npm run build` in the frontend directory."
            )
            # Fallback to a very basic HTML structure if the template is missing.
            return "<html><head><title>Error</title></head><body><h1>Application not built</h1><p>Please run 'npm run build' in the frontend directory.</p></body></html>"

        with open(self.index_template_path, "r") as f:
            template = f.read()

        try:
            with httpx.Client() as client:
                response = client.post(
                    self.render_server_url, json={"url": url}, timeout=5
                )
                response.raise_for_status()
                rendered_component = response.json()["html"]
        except httpx.RequestError as e:
            logger.error(
                "Could not connect to SSR render server: error_type=%s",
                type(e).__name__,
            )
            # Fallback to client-side rendering
            return template
        except Exception as e:
            logger.error(
                f"An unexpected error occurred while rendering the application: {type(e).__name__}"
            )
            # Fallback to client-side rendering
            return template

        # Inject the rendered component into the template
        return template.replace(
            '<div id="app"></div>', f'<div id="app">{rendered_component}</div>'
        )


# Singleton instance
base_dir = Path(__file__).resolve().parent.parent.parent
ssr_service = SSRService(base_dir)
