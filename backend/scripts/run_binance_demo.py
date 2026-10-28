"""Run the isolated read-only Futures Demo connector; never import app.main."""
import os

import uvicorn

from app.demo.main import create_app


def main():
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")), workers=1, access_log=False)


if __name__ == "__main__":
    main()
