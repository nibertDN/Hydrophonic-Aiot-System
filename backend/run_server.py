"""Start the backend with Waitress (works on Windows, Linux and macOS)."""
import os

from waitress import serve

from app import create_app

if __name__ == "__main__":
    app = create_app()
    port = int(os.environ.get("PORT", "5000"))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"Smart AI Irrigation running on http://localhost:{port}  (Ctrl+C to stop)")
    serve(app, host=host, port=port, threads=4)
