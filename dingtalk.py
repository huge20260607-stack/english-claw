"""钉钉发送模块：负责加签 + POST 到自定义机器人 Webhook。"""
import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request
import urllib.error


def _sign(secret):
    """钉钉加签：HMAC-SHA256(timestamp + "\n" + secret) -> base64 -> urlquote。"""
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return timestamp, sign


def send(webhook, secret, title, markdown_text):
    """发送一条 Markdown 消息到钉钉机器人。返回 True / False。"""
    url = webhook
    if secret:
        ts, sign = _sign(secret)
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}timestamp={ts}&sign={sign}"

    body = json.dumps({
        "msgtype": "markdown",
        "markdown": {"title": title, "text": markdown_text},
    }, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read())
            return payload.get("errcode") == 0
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, Exception) as e:
        # 这里不能用 raise，钉钉错误不能让云函数整体炸掉
        print(f"[dingtalk] send error: {type(e).__name__}: {e}")
        return False