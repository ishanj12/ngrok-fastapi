import uvicorn
from fastapi import FastAPI

import ngrok_fastapi

app = FastAPI()
ngrok_fastapi.attach(
    app,
    ngrok_fastapi.Config(
        port=8000,
        url="testregion.ngrok.app",
        traffic_policy="""
on_http_request:
  - actions:
      - type: basic-auth
        config:
          credentials:
            - "user:password123"
""",
    ),
)


@app.get("/")
def root():
    return {"message": "hello from ngrok-fastapi!"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
