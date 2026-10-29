"""Run the isolated read-only Futures Demo connector; never import app.main."""
import os

import uvicorn

from app.demo.main import create_app


def main():
    root = os.getenv("DEMO_STATE_ROOT", "/var/lib/capital-cipher-demo")
    if root != "/var/lib/capital-cipher-demo":
        raise RuntimeError("DEMO_STATE_PATH_INVALID")
    if os.getuid() == 0:
        os.makedirs(root, exist_ok=True)
        os.chown(root, 10001, 10001)
        os.setgid(10001)
        os.setuid(10001)
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")), workers=1, access_log=False)


if __name__ == "__main__":
    main()
