from app.core.security import hash_password, verify_password, create_access_token, decode_access_token

def test_password_hashing_round_trip():
    hashed = hash_password("StrongPassword123!")
    assert hashed != "StrongPassword123!"
    assert verify_password("StrongPassword123!", hashed)
    assert not verify_password("wrong-password", hashed)

def test_access_token_round_trip():
    token = create_access_token("user-123")
    assert decode_access_token(token) == "user-123"
