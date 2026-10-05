import re

SECRET_NAME_PATTERN = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASS|PRIVATE|CREDENTIAL)",
    re.IGNORECASE,
)


def is_probable_secret_name(name):
    return bool(SECRET_NAME_PATTERN.search(name))
