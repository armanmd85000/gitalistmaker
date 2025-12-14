import os

def must_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v

def opt_env(name: str, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default

class Config:
    API_ID = int(must_env("API_ID"))
    API_HASH = must_env("API_HASH")
    SESSION_STRING = must_env("SESSION_STRING")  # userbot session string
    OWNER_ID = int(must_env("OWNER_ID"))

    # Optional preset chats (can be set later via commands too)
    SOURCE_X = opt_env("SOURCE_X")

    TARGET1_A = opt_env("TARGET1_A")
    TARGET1_LIST = opt_env("TARGET1_LIST")

    TARGET2_A = opt_env("TARGET2_A")
    TARGET2_LIST = opt_env("TARGET2_LIST")

    DELAY_SECONDS = float(opt_env("DELAY_SECONDS", "0.4"))
