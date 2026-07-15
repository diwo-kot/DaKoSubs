import secrets
import string

def generate_secure_random_string(length=8, with_prefix=True):
    """Generates a secure random string with an optional '#' prefix."""
    characters = string.ascii_uppercase + string.digits
    random_part = ''.join(secrets.choice(characters) for _ in range(length))
    if with_prefix:
        return '#' + random_part
    return random_part