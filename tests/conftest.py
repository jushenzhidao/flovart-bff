"""pytest 夹具：在 import app 前固定环境变量。

测试结论只由代码和夹具决定 —— 不随开发者本机 .env / 真实 new-api 凭证而变。
故 BFF_SKIP_DOTENV=1，并把状态文件指向临时目录。
"""
import os
import tempfile

_DATA_DIR = tempfile.mkdtemp(prefix="flovart-bff-test-")

os.environ["BFF_SKIP_DOTENV"] = "1"
os.environ["BFF_SECRET_KEY"] = "t" * 64
os.environ["BFF_COOKIE_SECURE"] = "0"
os.environ["BFF_DATA_DIR"] = _DATA_DIR
os.environ["BFF_ADMIN_CRED_FILE"] = os.path.join(_DATA_DIR, "admin_cred.json")
os.environ["BFF_SIGNUP_STATE_FILE"] = os.path.join(_DATA_DIR, "signup_bonus.json")
# 指向本机快速失败端口：任何误触真实上游的调用会在 5s 连接超时内以 502 暴露
os.environ["NEWAPI_BASE_URL"] = "http://127.0.0.1:9"
os.environ["NEWAPI_ADMIN_PAT"] = "test-admin-pat"
os.environ["NEWAPI_ADMIN_UID"] = "1"
os.environ["NEWAPI_ADMIN_USERNAME"] = "test-admin"
os.environ["NEWAPI_ADMIN_PASSWORD"] = "test-admin-password"
