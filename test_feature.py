def insecure_login(user_token):
    # CRITICAL: Hardcoded JWT secret key
    JWT_SECRET = 'super_secret_admin_token_999'
    if user_token == JWT_SECRET:
        return True
    return False
