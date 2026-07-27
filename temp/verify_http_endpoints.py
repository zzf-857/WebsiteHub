import httpx

BASE_URL = "http://127.0.0.1:8100/api"

def main():
    client = httpx.Client(base_url=BASE_URL, headers={"Origin": "http://127.0.0.1:8100"})


    
    # 1. Login
    login_res = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
    print(f"[LOGIN STATUS]: {login_res.status_code}")
    assert login_res.status_code == 200, f"Login failed: {login_res.text}"
    print("[LOGIN COOKIES]:", client.cookies)

    # 2. Propose Reclassification via HTTP
    propose_res = client.post("/library/reclassify/propose")
    print(f"[PROPOSE STATUS]: {propose_res.status_code}")
    print("[PROPOSE RESPONSE JSON]:")
    print(propose_res.json())

if __name__ == "__main__":
    main()
