"""Local entry point: `python main.py` (or `uvicorn mysite.app:app --reload`)."""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("mysite.app:app", host="127.0.0.1", port=8000, reload=True)
