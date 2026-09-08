def insecure_login(user_token):
    # WARNING: Plaintext password comparison
    if user_token == 'admin_pass_123':
        return True
    return False
