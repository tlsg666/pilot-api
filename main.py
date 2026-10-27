from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
import uvicorn

app = FastAPI()


class LoginReq(BaseModel):
    email: str
    password: str

@app.post("/api/login")
@app.post("/api/login/")
def login(req: LoginReq):
    if req.password == "123456":
        return {"code": 0, "data": {"token": "this_is_a_fake_token_123456789"}}
    raise HTTPException(status_code=401, detail="密码错误")

@app.get("/api/list")
@app.get("/api/list/")
def get_list(page: int = 1, token: str = Header(None)):
    if token != "this_is_a_fake_token_123456789":
        raise HTTPException(status_code=401, detail="未授权")
    return {"code": 0, "data": {"items": [{"id": i, "name": f"item_{i}"} for i in range(1, 6)], "page": page}}

@app.post("/api/order")
@app.post("/api/order/")
def create_order():
    return {"code": 0, "data": {"order_id": "ORD-999"}}

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)