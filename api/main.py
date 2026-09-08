from fastapi import FastAPI

app = FastAPI(title="Unclaimed API")


@app.get("/health")
def health():
    return {"status": "ok"}
